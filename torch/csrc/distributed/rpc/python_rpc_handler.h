#pragma once

#include <torch/csrc/distributed/rpc/message.h>
#include <torch/csrc/distributed/rpc/types.h>
#include <torch/csrc/jit/frontend/script_type_parser.h>
#include <torch/csrc/utils/pybind.h>

namespace torch {
namespace distributed {
namespace rpc {

// Singleton class provides interface to execute python UDF remote call
// and deserialize the returned results by running python function
// in internal_rpc_utilities.
// The singleton object is constructed at first when RPC agent is
// constructed, where the python function in
// torch/distributed/internal_rpc_utils.py are imported only once.
class PYBIND11_EXPORT PythonRpcHandler {
 public:
  static PythonRpcHandler& getInstance();

  // Run a pickled Python UDF and return the result py::object
  py::object runPythonUdf(py::object&& pythonUdf);

  // Serialized a py::object into a string
  SerializedPyObj serialize(const py::object& obj);

  // Deserialize a string into a py::object
  py::object deserialize(const SerializedPyObj& serializedObj);

  // Check if obj is RemoteException, then throw it
  void handleException(const py::object& obj);
  // Alternative if the caller is already holding the GIL.
  void handleExceptionGILHeld(const py::object& obj);

  // Get QualifiedName string of a py::object.
  c10::QualifiedName getQualifiedName(const py::object& obj);

  // Explicitly clean up py::objects to avoid segment faults when
  // py::objects with CPython are cleaned up later at program exit
  // See similar issues reported https://github.com/pybind/pybind11/issues/1598
  // and https://github.com/pybind/pybind11/issues/1493
  // Our local tests also caught this segment faults if py::objects are cleaned
  // up at program exit. The explanation is: CPython cleans up most critical
  // utilities before cleaning up PythonRpcHandler singleton, so when
  // PythonRpcHandler singleton cleans up py::objects and call dec_ref(), it
  // will crash.
  // The solution is to clean up py::objects earlier when Rpc agent join().
  // Be note that py::objects can not be cleaned up when Rpc agent is destroyed
  // as well, as Rpc agent is global variable and it will have same issue as
  // PythonRpcHandler.
  void cleanup();

  std::shared_ptr<torch::jit::CompilationUnit> jitCompilationUnit();

  // Parse the string to recover the jit_type, this is used for RRef python
  // pickling/unpickling type recovery. The type string inference rule is as
  // follows:
  // 1. first try to parse if this is primitive types.
  //    i.e. TensorType, IntType, PyObjectType, etc.
  // 2. if not primitive type, we query the python_cu to see if it is a
  //    class type or interface type registered in python
  // We use a ScriptTypeParser instance with custom PythonTypeResolver
  // to resolve types according to the above rules.
  TypePtr parseTypeFromStr(const std::string& typeStr);

 private:
  PythonRpcHandler();
  ~PythonRpcHandler() = default;

  PythonRpcHandler(const PythonRpcHandler&) = delete;
  PythonRpcHandler& operator=(const PythonRpcHandler&) = delete;
  PythonRpcHandler(PythonRpcHandler&&) = delete;
  PythonRpcHandler& operator=(PythonRpcHandler&&) = delete;

  // Ref to `torch.distributed.rpc.internal._run_function`.
  py::object pyRunFunction_;

  // Ref to `torch.distributed.rpc.internal.serialize`.
  py::object pySerialize_;

  // Ref to `torch.distributed.rpc.internal.deserialize`.
  py::object pyDeserialize_;

  // Ref to 'torch.distributed.rpc.internal._handle_exception'
  py::object pyHandleException_;

  // Ref to 'torch.jit._qualified_name'
  py::object pyGetQualifiedName_;

  // Shared ptr to python compilation unit in jit, it is constructed in python
  // side (see _python_cu = torch._C.CompilationUnit() in jit/__init__.py)
  // and imported in C++ (see get_python_cu() in
  // csrc/jit/python/pybind_utils.h). We import the compilation unit here only
  // once for less cost and thread safety.
  std::shared_ptr<torch::jit::CompilationUnit> jitCompilationUnit_;

  // jit type parser to parse type_str back to TypePtr for RRef type
  // recovery when pickling and unpickling RRef
  std::shared_ptr<jit::ScriptTypeParser> typeParser_;
};

} // namespace rpc
} // namespace distributed
} // namespace torch
