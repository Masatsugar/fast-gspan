//! The command line is also the interface used by the Python wrapper.
use std::collections::VecDeque;
use std::str::FromStr;

pub const HELP: &str = "gBolt - frequent subgraph mining
Usage: gbolt -i FILE [options]
  -i, --input FILE                gSpan graph database
  -o, --output PREFIX             write one PREFIX.tN file per worker
  -s, --support FRACTION          relative support in (0, 1] (default 1)
  -x, --max-vertices N            maximum pattern vertices (0 = unlimited)
      --max-projection-size N     stop extending multi-edge patterns above N embeddings
      --time-limit SECONDS        save partial results after this budget (0 = unlimited)
      --resume FILE               resume from DFS codes in FILE
  -m, --mark CHARS                input field delimiters (default space)
  -p, --parents                   include parent IDs
  -d, --dfs                       include pattern vertices, DFS codes and graph IDs
  -n, --nodes                     write frequent individual vertex labels to PREFIX.nodes
  -h, --help                      display this help";

#[derive(Debug)]
pub struct Options {
    pub input: String,
    pub output: String,
    pub support: f64,
    pub max_vertices: usize,
    pub max_projection_size: usize,
    pub seconds: f64,
    pub resume: String,
    pub mark: String,
    pub parents: bool,
    pub dfs: bool,
    pub nodes: bool,
}

fn number<T: FromStr>(s: &str, option: &str) -> Result<T, String> {
    s.parse()
        .map_err(|_| format!("invalid value for {option}: {s}"))
}

pub fn parse(args: impl Iterator<Item = String>) -> Result<Option<Options>, String> {
    let mut options = Options {
        input: String::new(),
        output: String::new(),
        support: 1.0,
        max_vertices: 0,
        max_projection_size: 0,
        seconds: 0.0,
        resume: String::new(),
        mark: " ".into(),
        parents: false,
        dfs: false,
        nodes: false,
    };
    let mut args: VecDeque<_> = args.collect();
    if args.is_empty() {
        return Ok(None);
    }
    while let Some(arg) = args.pop_front() {
        let (name, attached) = if arg.starts_with("--") {
            match arg.split_once('=') {
                Some((key, value)) => (key, Some(value)),
                None => (&arg[..], None),
            }
        } else if arg.starts_with('-') && arg.len() >= 2 && arg.is_char_boundary(2) {
            let (key, suffix) = arg.split_at(2);
            // cxxopts accepts combined switches and attached short-option arguments.
            if suffix.is_empty() {
                (key, None)
            } else if matches!(key, "-n" | "-p" | "-d" | "-h") {
                args.push_front(format!("-{suffix}"));
                (key, None)
            } else {
                (key, Some(suffix))
            }
        } else {
            return Err(format!("invalid argument: {arg}"));
        };
        if matches!(name, "-h" | "--help") {
            return Ok(None);
        }
        let mut value = || {
            attached
                .map(str::to_owned)
                .or_else(|| args.pop_front())
                .ok_or_else(|| format!("{name} requires a value"))
        };
        let flag = || match attached.unwrap_or("true") {
            "true" | "1" => Ok(true),
            "false" | "0" => Ok(false),
            _ => Err(format!("invalid boolean for {name}")),
        };
        match name {
            "-i" | "--input" => options.input = value()?,
            "-o" | "--output" => options.output = value()?,
            "-s" | "--support" => options.support = number(&value()?, name)?,
            "-x" | "--max-vertices" => {
                options.max_vertices = number::<i32>(&value()?, name)?.max(0) as usize
            }
            "--max-projection-size" => options.max_projection_size = number(&value()?, name)?,
            "--time-limit" => options.seconds = number(&value()?, name)?,
            "--resume" => options.resume = value()?,
            "-m" | "--mark" => options.mark = value()?,
            "-p" | "--parents" => options.parents = flag()?,
            "-d" | "--dfs" => options.dfs = flag()?,
            "-n" | "--nodes" => options.nodes = flag()?,
            _ => return Err(format!("unknown option {name}")),
        }
    }
    if options.input.is_empty() {
        return Err("missing -i/--input".into());
    }
    if !(options.support > 0.0 && options.support <= 1.0) {
        return Err("support must be in (0, 1]".into());
    }
    if !options.seconds.is_finite() {
        return Err("time limit must be finite".into());
    }
    Ok(Some(options))
}
