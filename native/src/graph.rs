//! Read the gSpan transaction database, then prune labels by graph support.
use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::{self, BufRead};
use std::str::FromStr;

#[derive(Debug, Clone, Copy)]
pub struct Edge {
    pub to: usize,
    pub label: i32,
    pub id: usize,
}

#[derive(Debug, Default)]
pub struct Graph {
    pub id: usize,
    pub labels: Vec<i32>,
    pub adj: Vec<Vec<Edge>>,
}

#[derive(Default)]
struct Transaction {
    labels: Vec<i32>,
    edges: Vec<(usize, usize, i32)>,
}

pub struct Database {
    pub graphs: Vec<Graph>,
    pub min_support: usize,
    pub frequent_nodes: BTreeMap<i32, Vec<usize>>,
}

fn field<T: FromStr>(token: Option<&str>, name: &str) -> Result<T, String> {
    token
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| format!("invalid or missing {name}"))
}

fn label(token: Option<&str>) -> Result<i32, String> {
    let value = token.ok_or("missing graph label")?;
    if let Ok(label) = value.parse() {
        return Ok(label);
    }
    // NetworkX serializes labels as text. Preserve gBolt's atoi conversion
    // for values such as "4.0" or a categorical label without a numeric prefix.
    let digits = value
        .strip_prefix('-')
        .or_else(|| value.strip_prefix('+'))
        .unwrap_or(value);
    let digit_count = digits.bytes().take_while(u8::is_ascii_digit).count();
    let sign_len = value.len() - digits.len();
    if digit_count == 0 {
        return Ok(0);
    }
    value[..sign_len + digit_count]
        .parse()
        .map_err(|_| format!("label out of range: {value}"))
}

pub fn read_database(
    mut reader: impl BufRead,
    separator: &str,
    support: f64,
    save_nodes: bool,
) -> io::Result<Database> {
    let mut transactions = Vec::<Transaction>::new();
    let mut header_count = 0usize;
    let mut finished = false;
    let mut line = String::new();
    let mut line_no = 0;
    while {
        line.clear();
        reader.read_line(&mut line)? != 0
    } {
        line_no += 1;
        let mut tokens = line
            .trim()
            .split(|c: char| {
                if separator == " " {
                    c.is_ascii_whitespace()
                } else {
                    separator.contains(c)
                }
            })
            .filter(|s| !s.is_empty());
        let record = match tokens.next() {
            Some(value) => value,
            None => continue,
        };
        let mut parse = || -> Result<(), String> {
            if finished {
                return Err("unexpected record after t # -1".to_owned());
            }
            if record == "t" {
                if tokens.next() != Some("#") {
                    return Err("expected t # <graph-id>".to_owned());
                }
                let id: i64 = field(tokens.next(), "graph id")?;
                if id == -1 {
                    finished = true;
                } else if usize::try_from(id) == Ok(transactions.len()) {
                    transactions.push(Transaction::default());
                } else {
                    return Err("graph IDs must start at zero and be consecutive".to_owned());
                }
                header_count += 1;
                return Ok(());
            }
            let tx = transactions.last_mut().ok_or("expected a graph header")?;
            match record {
                "v" => {
                    let id: usize = field(tokens.next(), "vertex id")?;
                    if id != tx.labels.len() {
                        return Err("vertex IDs must start at zero and be consecutive".to_owned());
                    }
                    tx.labels.push(label(tokens.next())?);
                }
                "e" => {
                    let from = field(tokens.next(), "source vertex")?;
                    let to = field(tokens.next(), "target vertex")?;
                    if from >= tx.labels.len() || to >= tx.labels.len() {
                        return Err("edge refers to an unknown vertex".to_owned());
                    }
                    tx.edges.push((from, to, label(tokens.next())?));
                }
                _ => return Err(format!("unknown record {record}")),
            }
            Ok(())
        };
        parse().map_err(|e| {
            io::Error::new(io::ErrorKind::InvalidData, format!("line {line_no}: {e}"))
        })?;
    }
    // -s is a CLI frequency. The Python API targets an exact integer threshold
    // using this historical count, which includes the terminating header.
    let min_support = (header_count as f64 * support) as usize;
    let mut vertex_counts = HashMap::new();
    let mut edge_counts = HashMap::new();
    let mut frequent_nodes = BTreeMap::<i32, Vec<usize>>::new();
    let mut seen = HashSet::new();
    for (id, tx) in transactions.iter().enumerate() {
        seen.clear();
        for &label in &tx.labels {
            if seen.insert(label) {
                *vertex_counts.entry(label).or_insert(0usize) += 1;
                if save_nodes {
                    frequent_nodes.entry(label).or_default().push(id);
                }
            }
        }
        seen.clear();
        for &(_, _, label) in &tx.edges {
            if seen.insert(label) {
                *edge_counts.entry(label).or_insert(0usize) += 1;
            }
        }
    }
    frequent_nodes.retain(|_, ids| ids.len() >= min_support);
    let graphs = transactions
        .into_iter()
        .enumerate()
        .map(|(id, tx)| {
            let mut adj = vec![Vec::new(); tx.labels.len()];
            let mut edge_id = 0;
            for (from, to, label) in tx.edges {
                if edge_counts[&label] >= min_support
                    && vertex_counts[&tx.labels[from]] >= min_support
                    && vertex_counts[&tx.labels[to]] >= min_support
                {
                    adj[from].push(Edge {
                        to,
                        label,
                        id: edge_id,
                    });
                    adj[to].push(Edge {
                        to: from,
                        label,
                        id: edge_id,
                    });
                    edge_id += 1;
                }
            }
            Graph {
                id,
                labels: tx.labels,
                adj,
            }
        })
        .collect();
    Ok(Database {
        graphs,
        min_support,
        frequent_nodes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn networkx_label_strings_keep_legacy_integer_conversion() {
        assert_eq!(label(Some("-4.25")).unwrap(), -4);
        assert_eq!(label(Some("2suffix")).unwrap(), 2);
        assert_eq!(label(Some("C")).unwrap(), 0);
    }

    #[test]
    fn sentinel_counts_for_support_but_has_no_occurrences() {
        let input = b"t # 0\nv 0 4\nv 1 4\ne 0 1 9\nt # 1\nv 0 4\nt # -1\n";
        let database = read_database(&input[..], " ", 0.75, true).unwrap();
        assert_eq!(database.min_support, 2);
        assert_eq!(database.frequent_nodes[&4], [0, 1]);
        assert_eq!(database.graphs.len(), 2);
        assert!(database.graphs[0].adj[0].is_empty());
    }
}
