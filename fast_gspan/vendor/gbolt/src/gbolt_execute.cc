#include <gbolt.h>
#include <database.h>
#include <common.h>
#include <algorithm>
#include <fstream>
#include <sstream>
#ifdef __GLIBC__
#include <malloc.h>
#endif

namespace gbolt {

void GBolt::execute() {
  Database *database = Database::get_instance();
  vector<Graph> graphs;
  vector<Graph> prune_graphs;
  // Phase 1: construct an initial graph
  #ifdef GBOLT_PERFORMANCE
  struct timeval time_start, time_end;
  double elapsed = 0.0;
  CPU_TIMER_START(elapsed, time_start);
  #endif
  database->construct_graphs(graphs);
  nsupport_ = graphs.size() * support_;
  // TODO: find frequent edges
  find_frequent_nodes_and_edges(graphs);
  // Modified: 2026-11-14 - Mining only needs the pruned graphs. Release the
  // unpruned graphs here and the parsed input below; together they are
  // several times larger than the pruned graphs.
  vector<Graph>().swap(graphs);

  // Phase 2: prune the initial graph by frequent labels
  database->construct_graphs(frequent_vertex_labels_, frequent_edge_labels_, prune_graphs);
  database->release_input();
  #ifdef __GLIBC__
  // The freed blocks sit below the pruned graphs on the heap, so glibc does
  // not return them to the OS unless asked to.
  malloc_trim(0);
  #endif
  #ifdef GBOLT_PERFORMANCE
  CPU_TIMER_END(elapsed, time_start, time_end);
  LOG_INFO("gbolt construct graph time: %f", elapsed);
  CPU_TIMER_START(elapsed, time_start);
  #endif

  // Phase 3: graph mining
  init_instances(prune_graphs);
  project(prune_graphs);
  #ifdef GBOLT_PERFORMANCE
  CPU_TIMER_END(elapsed, time_start, time_end);
  LOG_INFO("gbolt mine graph time: %f", elapsed);
  #endif
}

void GBolt::init_instances(const vector<Graph> &graphs) {
  #ifdef GBOLT_SERIAL
  int num_threads = 1;
  #else
  int num_threads = omp_get_max_threads();
  #endif
  gbolt_instances_ = new gbolt_instance_t[num_threads];

  // Prepare history instance
  int max_edges = 0;
  int max_vertice = 0;
  for (auto i = 0; i < graphs.size(); ++i) {
    max_edges = std::max(graphs[i].get_nedges(), max_edges);
    max_vertice = std::max(
      static_cast<int>(graphs[i].get_p_vertice()->size()), max_vertice);
  }

  // Init an instance for each thread
  for (auto i = 0; i < num_threads; ++i) {
    #ifdef GBOLT_PERFORMANCE
    LOG_INFO("gbolt create thread %d", i);
    #endif
    string output_file_thread = output_file_ + ".t" + std::to_string(i);
    gbolt_instances_[i].history = new History(max_edges, max_vertice);
    gbolt_instances_[i].output = new Output(output_file_thread);
    gbolt_instances_[i].min_graph = new Graph();
    gbolt_instances_[i].min_dfs_codes = new DfsCodes();
    gbolt_instances_[i].right_most_path = new Path<int>(DEFAULT_PATH_LEN);
    gbolt_instances_[i].min_projection = new MinProjection(DEFAULT_PATH_LEN);
    gbolt_instances_[i].min_history = new History(max_edges, max_vertice);
    gbolt_instances_[i].min_right_most_path = new Path<int>(DEFAULT_PATH_LEN);
  }
}

// Modified: 2026-11-14 - Read the DFS codes of the patterns to resume from.
// Format: a "t ..." line starts a pattern, followed by one line per edge:
// e <from> <to> <from_label> <edge_label> <to_label>
void GBolt::load_resume(const string &resume_file) {
  std::ifstream fin(resume_file.c_str());
  if (!fin.is_open()) {
    LOG_ERROR("Open file error! %s", resume_file.c_str());
  }
  resume_root_.reset(new resume_node_t());
  resume_node_t *root = resume_root_.get();
  // Last node of the DFS code being read
  resume_node_t *node = NULL;
  string line;

  while (std::getline(fin, line)) {
    std::istringstream iss(line);
    string type;
    if (!(iss >> type)) {
      continue;
    }
    if (type == "t") {
      if (node != NULL && node != root) {
        node->unextended = true;
      }
      node = root;
    } else if (type == "e" && node != NULL) {
      dfs_code_t dfs_code;
      if (!(iss >> dfs_code.from >> dfs_code.to >> dfs_code.from_label
          >> dfs_code.edge_label >> dfs_code.to_label)) {
        LOG_ERROR("Reading resume file error!");
      }
      std::unique_ptr<resume_node_t> &child = node->children[dfs_code];
      if (!child) {
        child.reset(new resume_node_t());
      }
      node = child.get();
    } else {
      LOG_ERROR("Reading resume file error!");
    }
  }
  if (node != NULL && node != root) {
    node->unextended = true;
  }
}

void GBolt::project(const vector<Graph> &graphs) {
  ProjectionMap projection_map;
  // When resuming, only the single-edge patterns that lead to a pattern to
  // resume are needed
  const resume_node_t *resume_root = resume_root_.get();

  // Construct the first edge
  for (auto i = 0; i < graphs.size(); ++i) {
    const Graph &graph = graphs[i];

    for (auto j = 0; j < graph.size(); ++j) {
      const vertex_t *vertex = graph.get_p_vertex(j);
      Edges edges;

      if (get_forward_init(*vertex, graph, edges)) {
        for (auto k = 0; k < edges.size(); ++k) {
          const vertex_t *vertex_from = graph.get_p_vertex(edges[k]->from);
          const vertex_t *vertex_to = graph.get_p_vertex(edges[k]->to);
          // Push dfs code according to the same edge label
          dfs_code_t dfs_code(0, 1, vertex_from->label, edges[k]->label, vertex_to->label);
          if (resume_root != NULL && resume_root->child(dfs_code) == NULL) {
            continue;
          }
          // Push all the graphs
          projection_map[dfs_code].emplace_back(graphs[i].get_id(), edges[k], (const prev_dfs_t *)NULL);
        }
      }
    }
  }
  // Mine subgraphs
  int prev_graph_id = -1;
  #ifdef GBOLT_SERIAL
  int prev_thread_id = 1;
  #else
  int prev_thread_id = omp_get_thread_num();
  #endif
  DfsCodes dfs_codes;
  #ifndef GBOLT_SERIAL
  #pragma omp parallel
  #pragma omp single nowait
  #endif
  {
    for (auto it = projection_map.begin(); it != projection_map.end(); ++it) {
      // Parital pruning, like apriori
      Projection &projection = it->second;
      int nsupport = count_support(projection);
      if (nsupport < nsupport_) {
        continue;
      }
      int from_label = (it->first).from_label;
      int edge_label = (it->first).edge_label;
      int to_label = (it->first).to_label;
      const resume_node_t *child_resume =
        resume_root != NULL ? resume_root->child(it->first) : NULL;
      #ifdef GBOLT_SERIAL
      dfs_codes.emplace_back(&(it->first));
      mine_subgraph(graphs, projection, dfs_codes, nsupport, prev_thread_id, prev_graph_id,
        true, child_resume);
      dfs_codes.pop_back();
      #else
      #pragma omp task shared(graphs, projection, prev_thread_id, prev_graph_id) firstprivate(dfs_codes, nsupport, child_resume)
      {
        dfs_codes.emplace_back(&(it->first));
        mine_subgraph(graphs, projection, dfs_codes, nsupport, prev_thread_id, prev_graph_id,
          true, child_resume);
      }
      #endif
    }
  }
  #ifndef GBOLT_SERIAL
  #pragma omp taskwait
  #endif
}

}  // namespace gbolt
