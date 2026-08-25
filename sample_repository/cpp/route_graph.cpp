#include <queue>
#include <string>
#include <unordered_map>
#include <vector>

class RouteGraph {
 public:
  void add_edge(const std::string& from, const std::string& to) {
    edges_[from].push_back(to);
  }

  bool reachable(const std::string& origin, const std::string& destination) const {
    std::queue<std::string> frontier;
    std::unordered_map<std::string, bool> visited;
    frontier.push(origin);
    while (!frontier.empty()) {
      auto current = frontier.front();
      frontier.pop();
      if (current == destination) return true;
      if (visited[current]) continue;
      visited[current] = true;
      auto neighbors = edges_.find(current);
      if (neighbors != edges_.end()) {
        for (const auto& next : neighbors->second) frontier.push(next);
      }
    }
    return false;
  }

 private:
  std::unordered_map<std::string, std::vector<std::string>> edges_;
};
