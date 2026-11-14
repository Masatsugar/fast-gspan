// Modified: 2025-12-07 - Added max_num_vertices support
#ifndef INCLUDE_GBOLT_H_
#define INCLUDE_GBOLT_H_

#include <common.h>
#include <graph.h>
#include <history.h>
#include <path.h>
#include <output.h>
#include <atomic>
#include <chrono>
#include <map>
#include <memory>
#include <vector>
#include <string>

namespace gbolt {

// Tree of the DFS codes to resume from (see GBolt::load_resume)
struct resume_node_t {
  resume_node_t() : unextended(false) {}

  const resume_node_t *child(const dfs_code_t &dfs_code) const {
    auto it = children.find(dfs_code);
    return it == children.end() ? NULL : it->second.get();
  }

  // True for a pattern the previous run reported but did not extend. The
  // other nodes were already extended: they are only walked through again to
  // rebuild the projections of the patterns below them.
  bool unextended;
  map<dfs_code_t, std::unique_ptr<resume_node_t>, dfs_code_full_compare_t> children;
};

struct gbolt_instance_t {
  Graph *min_graph = NULL;
  DfsCodes *min_dfs_codes = NULL;
  History *history = NULL;
  Output *output = NULL;
  Path<int> *right_most_path = NULL;
  MinProjection *min_projection = NULL;
  // Scratch owned by is_min(), so that it can run while a projection is being
  // enumerated with history / right_most_path
  History *min_history = NULL;
  Path<int> *min_right_most_path = NULL;

  ~gbolt_instance_t() {
    delete this->min_graph;
    delete this->min_dfs_codes;
    delete this->history;
    delete this->output;
    delete this->right_most_path;
    delete this->min_projection;
    delete this->min_history;
    delete this->min_right_most_path;
  }
};

class GBolt {
 public:
  // Modified: 2025-12-07 - Added max_vertices parameter
  // Modified: 2026-11-14 - Added max_projection_size parameter (0 = unlimited)
  GBolt(const string &output_file, double support, int max_vertices = 0,
    size_t max_projection_size = 0) :
    output_file_(output_file), support_(support), max_num_vertices_(max_vertices),
    max_projection_size_(max_projection_size), num_stopped_by_size_(0),
    num_stopped_by_time_(0), time_limit_(0), time_limit_reached_(false),
    output_frequent_nodes_(0), gbolt_instances_(0) {}

  void execute();

  // Modified: 2026-11-14 - Stop extending patterns once `seconds` have passed
  // since `start` (0 = unlimited). Patterns reached after that are reported
  // but not extended.
  void set_time_limit(double seconds, std::chrono::steady_clock::time_point start) {
    time_limit_ = seconds;
    start_time_ = start;
  }

  // Modified: 2026-11-14 - Continue a previous run: mine only below the
  // patterns that run reported but did not extend (the "u: ..." entries of its
  // output), given as DFS codes in `resume_file`.
  void load_resume(const string &resume_file);

  // Numbers of frequent patterns that were reported but not extended. A
  // non-zero value means the mining result is incomplete.
  long num_stopped_by_size() const {
    return num_stopped_by_size_;
  }

  long num_stopped_by_time() const {
    return num_stopped_by_time_;
  }

  bool time_limit_reached() const {
    return time_limit_reached_;
  }

  void save(bool output_parent = false, bool output_pattern = false, bool output_frequent_nodes = false);

  ~GBolt() {
    if (gbolt_instances_ != 0) {
      delete[] gbolt_instances_;
    }
    if (output_frequent_nodes_ != 0) {
      delete output_frequent_nodes_;
    }
  }

 private:
  // Embeddings of one candidate extension. `accepted` is decided when the
  // extension is first seen: embeddings of a non-minimal (duplicate) DFS code,
  // or of one that is not on the way to a pattern to resume, are never stored.
  struct candidate_t {
    candidate_t() : accepted(false) {}
    Projection projection;
    bool accepted;
  };

  typedef map<dfs_code_t, Projection, dfs_code_project_compare_t> ProjectionMap;
  typedef map<dfs_code_t, candidate_t, dfs_code_backward_compare_t> ProjectionMapBackward;
  typedef map<dfs_code_t, candidate_t, dfs_code_forward_compare_t> ProjectionMapForward;

 private:
  // Mine
  void init_instances(const vector<Graph> &graphs);

  void project(const vector<Graph> &graphs);

  void find_frequent_nodes_and_edges(const vector<Graph> &graphs);

  void mine_subgraph(
    const vector<Graph> &graphs,
    const Projection &projection,
    DfsCodes &dfs_codes,
    int prev_nsupport,
    int prev_thread_id,
    int prev_graph_id,
    bool extend = true,
    const resume_node_t *resume = NULL);

  // True once the time limit has passed
  bool time_limit_exceeded() {
    if (time_limit_ <= 0) {
      return false;
    }
    if (time_limit_reached_) {
      return true;
    }
    std::chrono::duration<double> elapsed = std::chrono::steady_clock::now() - start_time_;
    if (elapsed.count() < time_limit_) {
      return false;
    }
    time_limit_reached_ = true;
    return true;
  }

  // Flag an already reported pattern as not extended
  void mark_unextended(Output *output, int index, stop_reason_t reason);

  // True when max_projection_size is set and this projection exceeds it
  bool exceeds_projection_limit(const Projection &projection) const {
    return max_projection_size_ > 0 && projection.size() > max_projection_size_;
  }

  // Extend
  void build_right_most_path(const DfsCodes &dfs_codes, Path<int> &right_most_path) {
    int prev_id = -1;

    for (auto i = dfs_codes.size(); i > 0; --i) {
      if (dfs_codes[i - 1]->from < dfs_codes[i - 1]->to &&
        (right_most_path.empty() || prev_id == dfs_codes[i - 1]->to)) {
        prev_id = dfs_codes[i - 1]->from;
        right_most_path.push_back(i - 1);
      }
    }
  }

  void update_right_most_path(const DfsCodes &dfs_codes, Path<int> &right_most_path) {
    auto *last_dfs_code = dfs_codes.back();
    // filter out a simple case
    if (last_dfs_code->from > last_dfs_code->to) {
      return;
    }
    right_most_path.reset();
    build_right_most_path(dfs_codes, right_most_path);
  }

  // Builds the projections of the extensions of `dfs_codes`. With `replay`
  // set, only the extensions that lead to a pattern to resume are built.
  // Returns false when the time limit passed before it finished.
  bool enumerate(
    const vector<Graph> &graphs,
    const DfsCodes &dfs_codes,
    const Projection &projection,
    const Path<int> &right_most_path,
    ProjectionMapBackward &projection_map_backward,
    ProjectionMapForward &projection_map_forward,
    bool enumerate_forward,
    const resume_node_t *replay);

  // Returns where to store an embedding of the extension `dfs_code`, or NULL
  // when the extension is not wanted: its DFS code is not minimal or, with
  // `replay` set, it does not lead to a pattern to resume.
  template <typename CandidateMap>
  Projection *candidate_projection(
    CandidateMap &candidates,
    const dfs_code_t &dfs_code,
    const DfsCodes &dfs_codes,
    const resume_node_t *replay) {
    auto it = candidates.find(dfs_code);
    if (it == candidates.end()) {
      it = candidates.insert(std::make_pair(dfs_code, candidate_t())).first;
      if (replay != NULL) {
        it->second.accepted = replay->child(dfs_code) != NULL;
      } else {
        DfsCodes extended(dfs_codes);
        extended.emplace_back(&(it->first));
        it->second.accepted = is_min(extended);
      }
    }
    return it->second.accepted ? &(it->second.projection) : NULL;
  }

  bool get_forward_init(
    const vertex_t &vertex,
    const Graph &graph,
    Edges &edges);

  void get_first_forward(
    const prev_dfs_t &prev_dfs,
    const History &history,
    const Graph &graph,
    const DfsCodes &dfs_codes,
    const Path<int> &right_most_path,
    ProjectionMapForward &projection_map_forward,
    const resume_node_t *replay);

  void get_other_forward(
    const prev_dfs_t &prev_dfs,
    const History &history,
    const Graph &graph,
    const DfsCodes &dfs_codes,
    const Path<int> &right_most_path,
    ProjectionMapForward &projection_map_forward,
    const resume_node_t *replay);

  void get_backward(
    const prev_dfs_t &prev_dfs,
    const History &history,
    const Graph &graph,
    const DfsCodes &dfs_codes,
    const Path<int> &right_most_path,
    ProjectionMapBackward &projection_map_backward,
    const resume_node_t *replay);

  // Count
  int count_support(const Projection &projection);

  void build_graph(const DfsCodes &dfs_codes, Graph &graph);

  bool is_min(const DfsCodes &dfs_codes);

  bool is_projection_min(
    const DfsCodes &dfs_codes,
    const Graph &min_graph,
    History &history,
    DfsCodes &min_dfs_codes,
    Path<int> &right_most_path,
    MinProjection &projection,
    size_t projection_start_index);

  bool judge_backward(
    const Path<int> &right_most_path,
    const Graph &min_graph,
    History &history,
    dfs_code_t &min_dfs_code,
    DfsCodes &min_dfs_codes,
    MinProjection &projection,
    size_t projection_start_index,
    size_t projection_end_index);

  bool judge_forward(
    const Path<int> &right_most_path,
    const Graph &min_graph,
    History &history,
    dfs_code_t &min_dfs_code,
    DfsCodes &min_dfs_codes,
    MinProjection &projection,
    size_t projection_start_index,
    size_t projection_end_index);

  // Report
  void report(const DfsCodes &dfs_codes, const Projection &projection,
    int nsupport, int prev_thread_id, int prev_graph_id);

 private:
  // Graphs after reconstructing
  vector<Graph> graphs_;
  // Single instance of minigraph
  #ifdef GBOLT_PERFORMANCE
  unordered_map<int, vector<int> > frequent_vertex_labels_;
  unordered_map<int, int> frequent_edge_labels_;
  #else
  map<int, vector<int> > frequent_vertex_labels_;
  map<int, int> frequent_edge_labels_;
  #endif
  string output_file_;
  double support_;
  int max_num_vertices_;  // Modified: 2025-12-07 - Added for vertex count filtering
  // Opt-in memory guard: patterns of two or more edges with more embeddings
  // than this are reported but not extended (0 = unlimited, i.e. complete
  // mining). Single-edge patterns are always extended.
  size_t max_projection_size_;
  long num_stopped_by_size_;
  long num_stopped_by_time_;
  // Wall-clock budget in seconds since start_time_ (0 = unlimited)
  double time_limit_;
  std::chrono::steady_clock::time_point start_time_;
  std::atomic<bool> time_limit_reached_;
  // Set when resuming a previous run
  std::unique_ptr<resume_node_t> resume_root_;
  int nsupport_;
  Output *output_frequent_nodes_;
  gbolt_instance_t *gbolt_instances_;
  dfs_code_project_compare_t dfs_code_project_compare_;
  dfs_code_forward_compare_t dfs_code_forward_compare_;
  dfs_code_backward_compare_t dfs_code_backward_compare_;
};

}  // namespace gbolt

#endif  // INCLUDE_GBOLT_H_
