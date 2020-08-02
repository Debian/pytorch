#include <torch/csrc/jit/passes/utils/subgraph_utils.h>

namespace torch {
namespace jit {
namespace SubgraphUtils {
namespace {

bool hasSubgraph(Node* n) {
  return n->hasAttribute(attr::Subgraph);
}

// Combine the nodes in two subgraph together. The nodes will end up in
// `mergeTo`, and `mergeFrom` is destroyed.
void mergeSubgraph(Node* mergeTo, Node* mergeFrom) {
  Node* nodeBeforeMergeFrom = mergeFrom->prev();
  Node* nodeAfterMergeFrom = mergeFrom->next();
  unmergeSubgraph(mergeFrom);
  std::vector<Node*> nodes;
  const auto end_it = nodeBeforeMergeFrom->reverseIterator();
  auto it = nodeAfterMergeFrom->reverseIterator();
  ++it;
  while (it != end_it) {
    // NB: mergeNodeIntoSubgraph destroys node, hence the complications
    Node* node = *it;
    ++it;
    mergeNodeIntoSubgraph(node, mergeTo);
  }
}
} // namespace

std::shared_ptr<Graph> getSubgraph(Node* n) {
  return n->g(attr::Subgraph);
}

void unmergeSubgraph(Node* subgraphNode) {
  // Inline the graph, replace uses of node outputs and destroy the node
  auto outerGraph = subgraphNode->owningGraph();
  WithInsertPoint guard(subgraphNode);
  const auto subgraphOutputs = insertGraph(
      *outerGraph, *getSubgraph(subgraphNode), subgraphNode->inputs());
  AT_ASSERT(subgraphOutputs.size() >= subgraphNode->outputs().size());
  for (size_t i = 0; i < subgraphNode->outputs().size(); ++i) {
    subgraphNode->outputs()[i]->replaceAllUsesWith(subgraphOutputs[i]);
  }
  subgraphNode->destroy();
}

void collectNestedUses(
    std::unordered_set<Value*>& closed_over_values,
    std::unordered_set<Value*>& new_values,
    std::unordered_map<Value*, Value*>& inputsMap,
    Node* input_node) {
  for (auto input : input_node->inputs()) {
    if (inputsMap.count(input) == 0 && new_values.count(input) == 0) {
      closed_over_values.insert(input);
    }
  }
  if (input_node->kind() == prim::If) {
    for (Block* block : input_node->blocks()) {
      for (Node* node : block->nodes()) {
        collectNestedUses(closed_over_values, new_values, inputsMap, node);
      }
      for (Value* v : block->outputs()) {
        if (inputsMap.count(v) == 0 && new_values.count(v) == 0) {
          closed_over_values.insert(v);
        }
      }
    }
  } else if (input_node->kind() == prim::Loop) {
    for (Value* v : input_node->inputs()) {
      if (inputsMap.count(v) == 0 && new_values.count(v) == 0) {
        closed_over_values.insert(v);
      }
    }
    Block* block = input_node->blocks().at(0);
    for (Value* v : block->inputs()) {
      new_values.insert(v);
    }
    for (Node* node : block->nodes()) {
      collectNestedUses(closed_over_values, new_values, inputsMap, node);
    }
  } else if (input_node->blocks().size() != 0) {
    TORCH_INTERNAL_ASSERT(false, input_node, " kind not handled yet");
  }
  for (Value* output : input_node->outputs()) {
    new_values.insert(output);
  }
}

std::unordered_set<Value*> closedOverValues(
    Node* toMerge,
    std::unordered_map<Value*, Value*>& inputsMap) {
  std::unordered_set<Value*> closed_over_values;
  std::unordered_set<Value*> new_values;
  collectNestedUses(closed_over_values, new_values, inputsMap, toMerge);
  return closed_over_values;
}

void mergeNodeIntoSubgraph(Node* toMerge, Node* subgraphNode) {
  AT_ASSERT(hasSubgraph(subgraphNode) && toMerge != subgraphNode);
  if (hasSubgraph(toMerge)) {
    return mergeSubgraph(subgraphNode, toMerge);
  }

  auto subgraph = getSubgraph(subgraphNode);

  // Map from values in the surrounding graph to inputs in the subgraph
  std::unordered_map<Value*, Value*> inputsMap;

  AT_ASSERT(subgraphNode->inputs().size() == subgraph->inputs().size());
  size_t idx = 0;
  for (auto input : subgraphNode->inputs()) {
    inputsMap[input] = subgraph->inputs()[idx];
    idx++;
  }

  // Add n's inputs to the group's input list if we don't already have them
  WithInsertPoint guard(*subgraph->nodes().begin());
  std::unordered_set<Value*> closedValues =
      closedOverValues(toMerge, inputsMap);

  // There are currently downstream usage that relies on a fixed ordering
  // of graph inputs. TODO: remove
  std::vector<Value*> orderedClosedValues;
  std::unordered_set<Value*> orderedSeenValues;
  for (Value* input : toMerge->inputs()) {
    orderedClosedValues.push_back(input);
    orderedSeenValues.insert(input);
  }
  for (Value* closedValue : closedValues) {
    if (!orderedSeenValues.count(closedValue)) {
      orderedClosedValues.push_back(closedValue);
      orderedSeenValues.insert(closedValue);
    }
  }

  for (auto input : orderedClosedValues) {
    if (inputsMap.count(input) == 0) {
      // Clone constants inside the subgraph instead of referencing them, to
      // enable more optimizations
      if (auto value = toIValue(input)) {
        auto nv = subgraph->insertConstant(*value);
        nv->setType(input->type()); // Need to retain type information on Nones
        inputsMap[input] = nv;
      } else {
        // The common case: this is a regular input, so just register it with
        // the group node and inner subgraph
        subgraphNode->addInput(input);
        auto inputToGraph = subgraph->addInput();
        inputToGraph->setType(input->type());
        inputsMap[input] = inputToGraph;
      }
    }
  }

  // Merge the node into the graph
  auto mergedNode = subgraph->insertNode(
      subgraph->createClone(toMerge, [&](Value* v) { return inputsMap[v]; }));

  // If n's outputs were inputs to `group`, remove them since we just merged
  // n in.
  //
  // i.e.,
  // x = f(w); group(x, y, z) becomes group(w, y, z).
  // x, y, z = f(w); group(x, y, z) becomes group(w).
  auto inputs = subgraphNode->inputs();
  for (size_t i = 0; i < toMerge->outputs().size(); ++i) {
    auto it = std::find(inputs.begin(), inputs.end(), toMerge->outputs()[i]);
    if (it != inputs.end()) {
      size_t p = it - inputs.begin();
      subgraphNode->removeInput(p);
      subgraph->inputs()[p]->replaceAllUsesWith(mergedNode->outputs()[i]);
      subgraph->eraseInput(p);
    }
  }

  // Add n's outputs to the group node and inner subgraph outputs.
  for (size_t i = 0; i < toMerge->outputs().size(); i++) {
    auto oldOutput = toMerge->outputs()[i];

    // Only register the output in the group node if it's actually used
    // outside the subgraph.
    const auto hasUsesOutsideSubgraph = std::any_of(
        oldOutput->uses().cbegin(),
        oldOutput->uses().cend(),
        [&](const Use& use) { return use.user->isAfter(subgraphNode); });

    if (hasUsesOutsideSubgraph) {
      auto newOutput = mergedNode->outputs()[i];
      subgraph->registerOutput(newOutput);
      auto groupOutput = subgraphNode->addOutput();
      groupOutput->copyMetadata(oldOutput);
      oldOutput->replaceAllUsesWith(groupOutput);
    }
  }
  // Remove the original node now that the merge is complete
  toMerge->destroy();
}

Node* createSingletonSubgraph(Node* n, Symbol subgraphKind) {
  auto graph = n->owningGraph();
  auto subgraph = graph->create(subgraphKind, 0);
  subgraph->g_(attr::Subgraph, std::make_shared<Graph>(graph->current_scope()));
  subgraph->insertBefore(n);
  mergeNodeIntoSubgraph(n, subgraph);
  return subgraph;
}

} // namespace SubgraphUtils
} // namespace jit
} // namespace torch
