#include <torch/csrc/jit/passes/normalize_ops.h>
#include <c10/util/Exception.h>

namespace torch {
namespace jit {

namespace {

// map from op alias -> normalized op
static const std::unordered_map<Symbol, Symbol> alias_map = {
    {aten::absolute, aten::abs},
    {aten::absolute_, aten::abs_},
};

void replaceNodeWithNewSymbol(Node* node, Symbol new_symbol) {
  WithInsertPoint insert_guard{node};
  auto graph = node->owningGraph();
  auto replace_node = graph->insertNode(graph->create(new_symbol, 0));
  for (Value* v : node->inputs()) {
    replace_node->addInput(v);
  }
  for (Value* v : node->outputs()) {
    auto new_out = replace_node->addOutput()->copyMetadata(v);
    v->replaceAllUsesWith(new_out);
  }
  replace_node->copyMetadata(node);
  TORCH_INTERNAL_ASSERT(
      replace_node->maybeOperator(),
      "invalid symbol replacement:",
      new_symbol,
      node->kind());
}

// having multiple ops in our IR that do the same thing makes the IR more
// difficult to consumer for downstream user of the IR, such as our own
// optimization passes here, we convert op aliases into a standard form
bool normalizeOpAliases(graph_node_list_iterator& iter) {
  auto alias = alias_map.find(iter->kind());
  if (alias != alias_map.end()) {
    replaceNodeWithNewSymbol(*iter, alias->second);
    iter.destroyCurrent();
    return true;
  }
  return false;
}

void NormalizeOps(Block* block) {
  for (auto it = block->nodes().begin(), end = block->nodes().end();
       it != end;) {
    for (auto sub : it->blocks()) {
      NormalizeOps(sub);
    }

    if (normalizeOpAliases(it)) {
      continue;
    }

    it++;
  }
}

} // namespace

void NormalizeOps(const std::shared_ptr<Graph>& graph) {
  NormalizeOps(graph->block());
}

} // namespace jit
} // namespace torch
