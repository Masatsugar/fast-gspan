mod cli;
mod graph;
mod miner;

use cli::Options;
use miner::{Code, Config, Sink};
use std::error::Error;
use std::fs::File;
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::time::{Duration, Instant};

fn read_resume(path: &str) -> Result<Vec<Code>, Box<dyn Error>> {
    let mut codes = Vec::new();
    for (i, line) in BufReader::new(File::open(path)?).lines().enumerate() {
        let line = line?;
        let tokens: Vec<_> = line.split_whitespace().collect();
        match tokens.as_slice() {
            [] => (),
            ["t", ..] => codes.push(Vec::new()),
            ["e", from, to, a, label, b] if !codes.is_empty() => {
                codes.last_mut().unwrap().push((
                    from.parse()?,
                    to.parse()?,
                    a.parse()?,
                    label.parse()?,
                    b.parse()?,
                ));
            }
            _ => return Err(format!("invalid DFS code in {path}, line {}", i + 1).into()),
        }
    }
    codes.retain(|code| !code.is_empty());
    Ok(codes)
}

fn write_ids(out: &mut impl Write, ids: &[usize]) -> io::Result<()> {
    write!(out, "x:")?;
    for id in ids {
        write!(out, " {id}")?;
    }
    writeln!(out, "\n")
}

// Each worker owns its output file. A completed pattern can be released as
// soon as its stop reason and parent ID are known.
struct FileSink<'a> {
    out: Option<BufWriter<File>>,
    options: &'a Options,
    size_stops: usize,
    time_stops: usize,
}

impl<'a> FileSink<'a> {
    fn new(options: &'a Options, worker: usize) -> io::Result<Self> {
        let out = if options.output.is_empty() {
            None
        } else {
            Some(BufWriter::new(File::create(format!(
                "{}.t{worker}",
                options.output
            ))?))
        };
        Ok(Self {
            out,
            options,
            size_stops: 0,
            time_stops: 0,
        })
    }
}

impl Sink for FileSink<'_> {
    fn emit(
        &mut self,
        id: usize,
        code: &Code,
        ids: &[usize],
        parent: Option<(usize, usize)>,
        reason: Option<&'static str>,
    ) -> io::Result<()> {
        match reason {
            Some("time_limit") => self.time_stops += 1,
            Some("max_projection_size") => self.size_stops += 1,
            _ => (),
        }
        let out = match &mut self.out {
            Some(out) => out,
            None => return Ok(()),
        };
        writeln!(out, "t # {id} * {}", ids.len())?;
        if self.options.parents {
            match parent {
                Some((worker, index)) => writeln!(out, "parent : {index} thread : {worker}")?,
                None => writeln!(out, "parent : -1")?,
            }
        }
        if let Some(reason) = reason {
            writeln!(out, "u: {reason}")?;
        }
        if self.options.dfs {
            // A forward DFS edge introduces the next vertex in ID order.
            writeln!(out, "v 0 {}", code[0].2)?;
            for &(from, to, _, _, label) in code {
                if from < to {
                    writeln!(out, "v {to} {label}")?;
                }
            }
            for (from, to, a, label, b) in code {
                writeln!(out, "e {from} {to} {a} {label} {b}")?;
            }
            write_ids(out, ids)?;
        }
        Ok(())
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let start = Instant::now();
    let options = match cli::parse(std::env::args().skip(1))? {
        Some(options) => options,
        None => {
            println!("{}", cli::HELP);
            return Ok(());
        }
    };
    let database = graph::read_database(
        BufReader::new(File::open(&options.input)?),
        &options.mark,
        options.support,
        options.nodes,
    )?;
    let resume = if options.resume.is_empty() {
        None
    } else {
        Some(read_resume(&options.resume)?)
    };
    let threads = std::env::var("OMP_NUM_THREADS")
        .ok()
        .and_then(|s| {
            s.split(',')
                .next()?
                .trim()
                .parse::<usize>()
                .ok()
                .filter(|n| *n > 0)
        })
        .unwrap_or_else(|| std::thread::available_parallelism().map_or(1, |n| n.get()));
    let deadline = if options.seconds > 0.0 && options.seconds < u64::MAX as f64 {
        start.checked_add(Duration::from_secs_f64(options.seconds))
    } else {
        None
    };
    let config = Config {
        min_support: database.min_support,
        max_vertices: options.max_vertices,
        max_projection_size: options.max_projection_size,
        deadline,
        resume,
    };
    let mut sinks = (0..threads)
        .map(|worker| FileSink::new(&options, worker))
        .collect::<io::Result<Vec<_>>>()?;
    let expired = miner::mine_with(&database.graphs, &config, &mut sinks)?;
    let mut size_stops = 0;
    let mut time_stops = 0;
    for sink in &mut sinks {
        size_stops += sink.size_stops;
        time_stops += sink.time_stops;
        if let Some(out) = &mut sink.out {
            out.flush()?;
        }
    }
    if size_stops + time_stops > 0 {
        eprintln!("gbolt: incomplete result: {} pattern(s) were not extended ({size_stops} over --max-projection-size, {time_stops} after --time-limit); their supergraphs may be missing", size_stops + time_stops);
    }
    if expired {
        eprintln!("gbolt: time limit reached");
    }
    if options.nodes && !options.output.is_empty() {
        let mut out = BufWriter::new(File::create(format!("{}.nodes", options.output))?);
        for (id, (label, ids)) in database.frequent_nodes.iter().enumerate() {
            writeln!(out, "t # {id} * {}\nv 0 {label}", ids.len())?;
            write_ids(&mut out, ids)?;
        }
        out.flush()?;
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("gbolt: {e}");
        std::process::exit(1);
    }
}
