//! Rightmost-path gSpan mining on undirected, vertex- and edge-labelled graphs.
//! Based on gBolt (Copyright 2017 Keren Zhou, BSD-2-Clause).
use crate::graph::{Edge, Graph};
use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap};
use std::io;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::Instant;

type Step = (usize, usize, i32, i32, i32);
pub type Code = Vec<Step>;

pub struct Config {
    pub min_support: usize,
    pub max_vertices: usize,
    pub max_projection_size: usize,
    pub deadline: Option<Instant>,
    pub resume: Option<Vec<Code>>,
}

/// One sink per worker. An emitted ID is local to that worker's output.
pub trait Sink {
    fn emit(
        &mut self,
        id: usize,
        code: &Code,
        ids: &[usize],
        parent: Option<(usize, usize)>,
        reason: Option<&'static str>,
    ) -> io::Result<()>;
}

// The child stores only its new edge and an index into the parent projection.
// Workers share ancestor projections until the last descendant is finished.
struct Embedding<'g> {
    graph: usize,
    from: usize,
    edge: &'g Edge,
    parent: usize,
}

struct Projection<'g> {
    entries: Vec<Embedding<'g>>,
    previous: Option<Arc<Projection<'g>>>,
}

fn graph_ids(projection: &Projection<'_>, graphs: &[Graph]) -> Vec<usize> {
    // Extensions are appended in parent order, so embeddings of each graph
    // remain contiguous, even when independent subtrees run in parallel.
    let mut ids = Vec::new();
    let mut previous = None;
    for embedding in &projection.entries {
        if previous != Some(embedding.graph) {
            ids.push(graphs[embedding.graph].id);
            previous = Some(embedding.graph);
        }
    }
    ids
}

// Bound the projection retained for a candidate until testing its canonicality.
// Small infrequent extensions can be discarded by support alone.
const CANONICALITY_BUFFER: usize = 64;

fn has_support(entries: &[Embedding<'_>], threshold: usize) -> bool {
    if entries.len() < threshold {
        return false;
    }
    let mut previous = None;
    let mut support = 0;
    for entry in entries {
        if previous != Some(entry.graph) {
            previous = Some(entry.graph);
            support += 1;
            if support >= threshold {
                return true;
            }
        }
    }
    false
}

fn rightmost_path(code: &[Step]) -> Vec<usize> {
    let mut path = Vec::new();
    let mut previous = None;
    for (i, &(from, to, ..)) in code.iter().enumerate().rev() {
        if from < to && previous.map_or(true, |id| id == to) {
            path.push(i);
            previous = Some(from);
        }
    }
    path
}

#[derive(Default)]
struct History {
    nodes: Vec<usize>,
    edges: Vec<usize>,
    visited: Vec<usize>,
    stamp: usize,
}

impl History {
    fn build(&mut self, code: &[Step], projection: &Projection<'_>, mut index: usize, n: usize) {
        if self.visited.len() < n {
            self.visited.resize(n, 0);
        }
        self.stamp = self.stamp.wrapping_add(1);
        if self.stamp == 0 {
            self.visited.fill(0);
            self.stamp = 1;
        }
        // A connected code of k edges uses at most k + 1 vertices.
        self.nodes.resize(code.len() + 1, 0);
        self.edges.resize(code.len(), 0);
        let mut layer = projection;
        for (i, &(from, to, ..)) in code.iter().enumerate().rev() {
            let entry = &layer.entries[index];
            self.nodes[from] = entry.from;
            self.nodes[to] = entry.edge.to;
            self.edges[i] = entry.edge.id;
            self.visited[entry.from] = self.stamp;
            self.visited[entry.edge.to] = self.stamp;
            if let Some(previous) = &layer.previous {
                layer = previous;
                index = entry.parent;
            }
        }
    }

    fn extensions<'g>(
        &self,
        graph: &'g Graph,
        code: &[Step],
        path: &[usize],
        forward: bool,
        mut visit: impl FnMut(Step, usize, &'g Edge),
    ) {
        let right = code[path[0]].1;
        let tip = self.nodes[right];
        let tip_label = graph.labels[tip];
        // A backward edge closes a cycle with a rightmost-path ancestor.
        for &i in path[1..].iter().rev() {
            let (ancestor, _, ancestor_label, edge_label, next_label) = code[i];
            for edge in &graph.adj[tip] {
                if edge.to == self.nodes[ancestor]
                    && (edge.label, tip_label) >= (edge_label, next_label)
                    && !self.edges.contains(&edge.id)
                {
                    visit(
                        (right, ancestor, tip_label, edge.label, ancestor_label),
                        tip,
                        edge,
                    );
                }
            }
        }
        if !forward {
            return;
        }

        for edge in &graph.adj[tip] {
            let label = graph.labels[edge.to];
            if self.visited[edge.to] != self.stamp && label >= code[0].2 {
                visit((right, right + 1, tip_label, edge.label, label), tip, edge);
            }
        }
        for &i in path {
            let (source, _, source_label, tree_edge_label, child_label) = code[i];
            let from = self.nodes[source];
            for edge in &graph.adj[from] {
                let label = graph.labels[edge.to];
                if self.visited[edge.to] != self.stamp
                    && label >= code[0].2
                    && (edge.label, label) >= (tree_edge_label, child_label)
                {
                    visit(
                        (source, right + 1, source_label, edge.label, label),
                        from,
                        edge,
                    );
                }
            }
        }
    }
}

fn extension_order(a: Step, b: Step) -> Ordering {
    match (a.0 > a.1, b.0 > b.1) {
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
        (true, true) => (a.1, a.3).cmp(&(b.1, b.3)),
        (false, false) => b.0.cmp(&a.0).then_with(|| (a.3, a.4).cmp(&(b.3, b.4))),
    }
}

fn graph_from_code(code: &[Step]) -> Graph {
    let n = code.iter().map(|s| s.0.max(s.1)).max().unwrap() + 1;
    let mut graph = Graph {
        id: 0,
        labels: vec![0; n],
        adj: (0..n).map(|_| Vec::new()).collect(),
    };
    for (id, &(from, to, from_label, label, to_label)) in code.iter().enumerate() {
        graph.labels[from] = from_label;
        graph.labels[to] = to_label;
        graph.adj[from].push(Edge { to, label, id });
        graph.adj[to].push(Edge {
            to: from,
            label,
            id,
        });
    }
    graph
}

// Recreate the lexicographically first DFS traversal of the candidate graph.
// At each prefix we need only embeddings of the proposed next step. A single
// smaller eligible extension disqualifies the whole candidate immediately.
fn is_minimal(code: &[Step]) -> bool {
    let graph = graph_from_code(code);
    let mut roots = Vec::new();
    for (from, edges) in graph.adj.iter().enumerate() {
        for edge in edges {
            let step = (0, 1, graph.labels[from], edge.label, graph.labels[edge.to]);
            if step.2 > step.4 {
                continue;
            }
            match step.cmp(&code[0]) {
                Ordering::Less => return false,
                Ordering::Equal => roots.push(Embedding {
                    graph: 0,
                    from,
                    edge,
                    parent: 0,
                }),
                Ordering::Greater => {}
            }
        }
    }
    let mut projection = Projection {
        entries: roots,
        previous: None,
    };
    let mut history = History::default();
    for depth in 1..code.len() {
        let prefix = &code[..depth];
        let expected = code[depth];
        let path = rightmost_path(prefix);
        let mut entries = Vec::new();
        for i in 0..projection.entries.len() {
            history.build(prefix, &projection, i, graph.labels.len());
            let mut smaller = false;
            history.extensions(
                &graph,
                prefix,
                &path,
                true,
                |step, from, edge| match extension_order(step, expected) {
                    Ordering::Less => smaller = true,
                    Ordering::Equal if step == expected => {
                        entries.push(Embedding {
                            graph: 0,
                            from,
                            edge,
                            parent: i,
                        });
                    }
                    _ => {}
                },
            );
            if smaller {
                return false;
            }
        }
        if entries.is_empty() {
            return false;
        }
        projection = Projection {
            entries,
            previous: Some(Arc::new(projection)),
        };
    }
    true
}

#[derive(Default)]
struct ResumeNode {
    pending: bool,
    children: HashMap<Step, usize>,
}

fn resume_tree(codes: &[Code]) -> Vec<ResumeNode> {
    let mut tree = vec![ResumeNode::default()];
    for code in codes {
        let mut node = 0;
        for &step in code {
            node = match tree[node].children.get(&step) {
                Some(&existing) => existing,
                None => {
                    let new = tree.len();
                    tree.push(ResumeNode::default());
                    tree[node].children.insert(step, new);
                    new
                }
            };
        }
        tree[node].pending = true;
    }
    tree
}

struct Task<'g> {
    projection: Projection<'g>,
    code: Code,
    parent: Option<(usize, usize)>,
    replay: Option<usize>,
}

struct Miner<'a> {
    config: &'a Config,
    graphs: &'a [Graph],
    resume: Vec<ResumeNode>,
}

struct Worker<'s, S> {
    id: usize,
    emitted: usize,
    sink: &'s mut S,
    history: History,
    timed_out: bool,
}

impl<'s, S: Sink> Worker<'s, S> {
    fn new(id: usize, sink: &'s mut S) -> Self {
        Worker {
            id,
            emitted: 0,
            sink,
            history: History::default(),
            timed_out: false,
        }
    }

    fn expired(&mut self, deadline: Option<Instant>) -> bool {
        self.timed_out |= deadline.map_or(false, |d| Instant::now() >= d);
        self.timed_out
    }

    fn expand<'g>(&mut self, miner: &Miner<'g>, task: Task<'g>) -> io::Result<Vec<Task<'g>>> {
        let Task {
            projection,
            mut code,
            parent,
            replay,
        } = task;
        let config = miner.config;
        let ids = graph_ids(&projection, miner.graphs);
        if ids.len() < config.min_support {
            return Ok(Vec::new());
        }
        let parent_id = replay.is_none().then_some((self.id, self.emitted));
        if replay.is_none()
            && code.len() > 1
            && config.max_projection_size > 0
            && projection.entries.len() > config.max_projection_size
        {
            self.sink.emit(
                self.emitted,
                &code,
                &ids,
                parent,
                Some("max_projection_size"),
            )?;
            self.emitted += 1;
            return Ok(Vec::new());
        }
        if self.expired(config.deadline) {
            if replay.is_none() {
                self.sink
                    .emit(self.emitted, &code, &ids, parent, Some("time_limit"))?;
                self.emitted += 1;
            }
            return Ok(Vec::new());
        }

        let path = rightmost_path(&code);
        let right = code[path[0]].1;
        let forward = config.max_vertices == 0 || right + 1 < config.max_vertices;
        // A missing bucket records a rejected DFS code. Small buckets wait for
        // support to be known; large ones must pass canonicality to keep growing.
        let mut candidates = BTreeMap::<Step, Option<Vec<Embedding>>>::new();
        let mut interrupted = false;
        if forward || path.len() > 1 {
            for (i, embedding) in projection.entries.iter().enumerate() {
                if i & 4095 == 4095 && self.expired(config.deadline) {
                    interrupted = true;
                    break;
                }
                let graph = &miner.graphs[embedding.graph];
                self.history
                    .build(&code, &projection, i, graph.labels.len());
                self.history
                    .extensions(graph, &code, &path, forward, |step, from, edge| {
                        let accepted = candidates.entry(step).or_insert_with(|| {
                            if let Some(node) = replay {
                                if !miner.resume[node].children.contains_key(&step) {
                                    return None;
                                }
                            }
                            Some(Vec::new())
                        });
                        if let Some(entries) = accepted {
                            if entries.len() == CANONICALITY_BUFFER {
                                let mut candidate = code.clone();
                                candidate.push(step);
                                if !is_minimal(&candidate) {
                                    *accepted = None;
                                    return;
                                }
                            }
                            entries.push(Embedding {
                                graph: embedding.graph,
                                from,
                                edge,
                                parent: i,
                            });
                        }
                    });
            }
        }
        if replay.is_none() {
            let reason = interrupted.then_some("time_limit");
            self.sink.emit(self.emitted, &code, &ids, parent, reason)?;
            self.emitted += 1;
        }
        if interrupted || candidates.is_empty() {
            return Ok(Vec::new());
        }

        let previous = Arc::new(projection);
        let mut children = Vec::new();
        for (step, bucket) in candidates {
            let entries = match bucket {
                Some(entries) if has_support(&entries, config.min_support) => entries,
                _ => continue,
            };
            code.push(step);
            if entries.len() > CANONICALITY_BUFFER || is_minimal(&code) {
                let child = replay.and_then(|node| miner.resume[node].children.get(&step).copied());
                children.push(Task {
                    projection: Projection {
                        entries,
                        previous: Some(Arc::clone(&previous)),
                    },
                    code: code.clone(),
                    parent: parent_id,
                    replay: child.filter(|&node| !miner.resume[node].pending),
                });
            }
            code.pop();
        }
        Ok(children)
    }
}

fn root_tasks<'g>(miner: &Miner<'g>) -> Vec<Task<'g>> {
    if miner.config.max_vertices == 1 {
        return Vec::new();
    }
    let mut roots = BTreeMap::<Step, Vec<Embedding>>::new();
    for (gid, graph) in miner.graphs.iter().enumerate() {
        for (from, edges) in graph.adj.iter().enumerate() {
            for edge in edges {
                let step = (0, 1, graph.labels[from], edge.label, graph.labels[edge.to]);
                if step.2 <= step.4
                    && (miner.resume.is_empty() || miner.resume[0].children.contains_key(&step))
                {
                    roots.entry(step).or_default().push(Embedding {
                        graph: gid,
                        from,
                        edge,
                        parent: 0,
                    });
                }
            }
        }
    }
    roots
        .into_iter()
        .rev()
        .map(|(step, entries)| Task {
            code: vec![step],
            projection: Projection {
                entries,
                previous: None,
            },
            parent: None,
            replay: miner
                .resume
                .first()
                .and_then(|root| root.children.get(&step))
                .copied()
                .filter(|&node| !miner.resume[node].pending),
        })
        .collect()
}

struct Queue<'g> {
    ready: Vec<Task<'g>>,
    busy: usize,
    failed: bool,
}

fn parallel<'g, S: Sink + Send>(
    miner: &Miner<'g>,
    roots: Vec<Task<'g>>,
    sinks: &mut [S],
) -> io::Result<bool> {
    let threads = sinks.len();
    let queue = Mutex::new(Queue {
        ready: roots,
        busy: 0,
        failed: false,
    });
    let changed = Condvar::new();
    thread::scope(|scope| {
        let mut handles = Vec::new();
        for (id, sink) in sinks.iter_mut().enumerate() {
            let queue = &queue;
            let changed = &changed;
            handles.push(scope.spawn(move || {
                let mut worker = Worker::new(id, sink);
                loop {
                    let mut guard = queue.lock().unwrap();
                    let root = loop {
                        if guard.failed {
                            return Ok(worker.timed_out);
                        }
                        if let Some(task) = guard.ready.pop() {
                            guard.busy += 1;
                            break task;
                        }
                        if guard.busy == 0 {
                            return Ok(worker.timed_out);
                        }
                        guard = changed.wait(guard).unwrap();
                    };
                    drop(guard);

                    let mut local = vec![root];
                    while let Some(task) = local.pop() {
                        match worker.expand(miner, task) {
                            Ok(children) => local.extend(children),
                            Err(error) => {
                                // Wake idle workers even when this was the last
                                // outstanding task. The original I/O error is
                                // returned by this thread's join handle.
                                queue.lock().unwrap().failed = true;
                                changed.notify_all();
                                return Err(error);
                            }
                        }
                        if local.len() > 1 {
                            let mut guard = queue.lock().unwrap();
                            if guard.failed {
                                return Ok(worker.timed_out);
                            }
                            // Offer siblings to the shared queue; keep the
                            // current DFS branch and its projections local.
                            let n =
                                (local.len() - 1).min(threads.saturating_sub(guard.ready.len()));
                            if n > 0 {
                                guard.ready.extend(local.drain(..n));
                                changed.notify_all();
                            }
                        }
                    }
                    let mut guard = queue.lock().unwrap();
                    guard.busy -= 1;
                    if guard.busy == 0 {
                        changed.notify_all();
                    }
                }
            }));
        }
        let mut timed_out = false;
        for handle in handles {
            timed_out |= handle.join().unwrap()?;
        }
        Ok(timed_out)
    })
}

pub fn mine_with<S: Sink + Send>(
    graphs: &[Graph],
    config: &Config,
    sinks: &mut [S],
) -> io::Result<bool> {
    if sinks.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "expected a worker sink",
        ));
    }
    let resume = config
        .resume
        .as_ref()
        .map_or_else(Vec::new, |codes| resume_tree(codes));
    let miner = Miner {
        config,
        graphs,
        resume,
    };
    let mut roots = root_tasks(&miner);
    if sinks.len() == 1 || roots.is_empty() {
        let mut worker = Worker::new(0, &mut sinks[0]);
        while let Some(task) = roots.pop() {
            roots.extend(worker.expand(&miner, task)?);
        }
        Ok(worker.timed_out)
    } else {
        parallel(&miner, roots, sinks)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Pattern {
        code: Code,
        graph_ids: Vec<usize>,
        stop_reason: Option<&'static str>,
        parent: Option<(usize, usize)>,
    }

    impl Sink for Vec<Pattern> {
        fn emit(
            &mut self,
            id: usize,
            code: &Code,
            ids: &[usize],
            parent: Option<(usize, usize)>,
            reason: Option<&'static str>,
        ) -> io::Result<()> {
            assert_eq!(id, self.len());
            self.push(Pattern {
                code: code.clone(),
                graph_ids: ids.to_vec(),
                stop_reason: reason,
                parent,
            });
            Ok(())
        }
    }

    fn mine(graphs: &[Graph], config: &Config, threads: usize) -> (Vec<Vec<Pattern>>, bool) {
        let mut outputs: Vec<_> = (0..threads).map(|_| Vec::new()).collect();
        let timed_out = mine_with(graphs, config, &mut outputs).unwrap();
        (outputs, timed_out)
    }

    fn config() -> Config {
        Config {
            min_support: 1,
            max_vertices: 3,
            max_projection_size: 0,
            deadline: None,
            resume: None,
        }
    }

    #[test]
    fn vertex_limit_permits_backward_edge_and_support_counts_graphs() {
        let triangle = vec![(0, 1, 1, 0, 1), (1, 2, 1, 0, 1), (2, 0, 1, 0, 1)];
        let mut graphs = vec![graph_from_code(&triangle), graph_from_code(&triangle[..2])];
        graphs[1].id = 1;
        for threads in [1, 2] {
            let (workers, timed_out) = mine(&graphs, &config(), threads);
            assert!(!timed_out);
            for pattern in workers.iter().flatten() {
                if let Some((shard, id)) = pattern.parent {
                    assert_eq!(
                        workers[shard][id].code,
                        pattern.code[..pattern.code.len() - 1]
                    );
                } else {
                    assert_eq!(pattern.code.len(), 1);
                }
            }
            let result: BTreeMap<_, _> = workers
                .into_iter()
                .flatten()
                .map(|pattern| (pattern.code, pattern.graph_ids))
                .collect();
            assert_eq!(result.len(), 3);
            assert_eq!(result[&triangle], [0]);
            assert_eq!(result[&triangle[..2]], [0, 1]);
            assert_eq!(result[&triangle[..1]], [0, 1]);
        }
    }

    #[test]
    fn oversized_projection_is_a_resumable_frontier() {
        let triangle = vec![(0, 1, 2, 0, 2), (1, 2, 2, 0, 2), (2, 0, 2, 0, 2)];
        let graphs = vec![graph_from_code(&triangle)];
        let mut settings = config();
        settings.max_projection_size = 5;
        let (partial, _) = mine(&graphs, &settings, 1);
        let frontier: Vec<_> = partial
            .into_iter()
            .flatten()
            .filter(|p| p.stop_reason == Some("max_projection_size"))
            .map(|p| p.code)
            .collect();
        assert_eq!(frontier, [triangle[..2].to_vec()]);
        settings.max_projection_size = 6;
        settings.resume = Some(frontier);
        let (resumed, _) = mine(&graphs, &settings, 1);
        assert!(resumed[0].iter().all(|p| p.stop_reason.is_none()));
        assert_eq!(resumed[0].len(), 2);
        assert_eq!(resumed[0][1].code, triangle);
    }

    #[test]
    fn sink_error_reaches_caller_and_releases_idle_workers() {
        struct Fails;
        impl Sink for Fails {
            fn emit(
                &mut self,
                _: usize,
                _: &Code,
                _: &[usize],
                _: Option<(usize, usize)>,
                _: Option<&'static str>,
            ) -> io::Result<()> {
                Err(io::Error::new(io::ErrorKind::BrokenPipe, "output closed"))
            }
        }
        let graphs = vec![graph_from_code(&[(0, 1, 1, 0, 1)])];
        let error = mine_with(&graphs, &config(), &mut [Fails, Fails, Fails]).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
    }

    #[test]
    fn canonicality_at_the_candidate_buffer_boundary() {
        // Each disconnected three-vertex path has exactly two orientations.
        // The same path also admits a noncanonical DFS code rooted at its center.
        let canonical = vec![(0, 1, 1, 0, 1), (1, 2, 1, 0, 1)];
        for copies in [CANONICALITY_BUFFER / 2, CANONICALITY_BUFFER / 2 + 1] {
            let mut graph = Graph {
                id: 0,
                labels: vec![1; 3 * copies],
                adj: vec![Vec::new(); 3 * copies],
            };
            for path in 0..copies {
                for offset in 0..2 {
                    let from = 3 * path + offset;
                    let to = from + 1;
                    let id = 2 * path + offset;
                    graph.adj[from].push(Edge { to, label: 0, id });
                    graph.adj[to].push(Edge {
                        to: from,
                        label: 0,
                        id,
                    });
                }
            }
            let (output, _) = mine(&[graph], &config(), 1);
            assert_eq!(output[0].len(), 2);
            assert_eq!(output[0][1].code, canonical);
        }
    }

    #[test]
    fn noncanonical_resume_code_cannot_create_a_duplicate_pattern() {
        let path = vec![(0, 1, 1, 0, 1), (1, 2, 1, 0, 1)];
        let graphs = vec![graph_from_code(&path)];
        let mut settings = config();
        settings.resume = Some(vec![vec![(0, 1, 1, 0, 1), (0, 2, 1, 0, 1)]]);
        let (resumed, _) = mine(&graphs, &settings, 1);
        assert!(resumed[0].is_empty());
    }
}
