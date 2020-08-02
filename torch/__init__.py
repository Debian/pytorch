
r"""
The torch package contains data structures for multi-dimensional
tensors and mathematical operations over these are defined.
Additionally, it provides many utilities for efficient serializing of
Tensors and arbitrary types, and other useful utilities.

It has a CUDA counterpart, that enables you to run your tensor computations
on an NVIDIA GPU with compute capability >= 3.0.
"""

import os
import sys
import platform
import ctypes

if sys.version_info < (3,):
    raise Exception("Python 2 has reached end-of-life and is no longer supported by PyTorch.")

from ._utils import _import_dotted_name
from ._utils_internal import get_file_path, prepare_multiprocessing_environment, \
    USE_RTLD_GLOBAL_WITH_LIBTORCH, USE_GLOBAL_DEPS
from .version import __version__
from ._six import string_classes as _string_classes

from typing import Set, Type

__all__ = [
    'typename', 'is_tensor', 'is_storage', 'set_default_tensor_type',
    'set_rng_state', 'get_rng_state', 'manual_seed', 'initial_seed', 'seed',
    'save', 'load', 'set_printoptions', 'chunk', 'split', 'stack', 'matmul',
    'no_grad', 'enable_grad', 'rand', 'randn',
    'DoubleStorage', 'FloatStorage', 'LongStorage', 'IntStorage',
    'ShortStorage', 'CharStorage', 'ByteStorage', 'BoolStorage',
    'DoubleTensor', 'FloatTensor', 'LongTensor', 'IntTensor',
    'ShortTensor', 'CharTensor', 'ByteTensor', 'BoolTensor', 'Tensor',
    'lobpcg', '_set_deterministic', '_is_deterministic'
]

################################################################################
# Load the extension module
################################################################################

if sys.platform == 'win32':
    pfiles_path = os.getenv('ProgramFiles', 'C:\\Program Files')
    py_dll_path = os.path.join(sys.exec_prefix, 'Library', 'bin')
    th_dll_path = os.path.join(os.path.dirname(__file__), 'lib')

    # When users create a virtualenv that inherits the base environment,
    # we will need to add the corresponding library directory into
    # DLL search directories. Otherwise, it will rely on `PATH` which
    # is dependent on user settings.
    if sys.exec_prefix != sys.base_exec_prefix:
        base_py_dll_path = os.path.join(sys.base_exec_prefix, 'Library', 'bin')
    else:
        base_py_dll_path = ''

    dll_paths = list(filter(os.path.exists, [th_dll_path, py_dll_path, base_py_dll_path]))

    if all([not os.path.exists(os.path.join(p, 'nvToolsExt64_1.dll')) for p in dll_paths]):
        nvtoolsext_dll_path = os.path.join(
            os.getenv('NVTOOLSEXT_PATH', os.path.join(pfiles_path, 'NVIDIA Corporation', 'NvToolsExt')), 'bin', 'x64')
    else:
        nvtoolsext_dll_path = ''

    from .version import cuda as cuda_version
    import glob
    if cuda_version and all([not glob.glob(os.path.join(p, 'cudart64*.dll')) for p in dll_paths]):
        cuda_version_1 = cuda_version.replace('.', '_')
        cuda_path_var = 'CUDA_PATH_V' + cuda_version_1
        default_path = os.path.join(pfiles_path, 'NVIDIA GPU Computing Toolkit', 'CUDA', 'v' + cuda_version)
        cuda_path = os.path.join(os.getenv(cuda_path_var, default_path), 'bin')
    else:
        cuda_path = ''

    dll_paths.extend(filter(os.path.exists, [nvtoolsext_dll_path, cuda_path]))

    kernel32 = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    with_load_library_flags = hasattr(kernel32, 'AddDllDirectory')
    prev_error_mode = kernel32.SetErrorMode(0x0001)

    kernel32.LoadLibraryW.restype = ctypes.c_void_p
    if with_load_library_flags:
        kernel32.AddDllDirectory.restype = ctypes.c_void_p
        kernel32.LoadLibraryExW.restype = ctypes.c_void_p

    for dll_path in dll_paths:
        if sys.version_info >= (3, 8):
            os.add_dll_directory(dll_path)
        elif with_load_library_flags:
            res = kernel32.AddDllDirectory(dll_path)
            if res is None:
                err = ctypes.WinError(ctypes.get_last_error())
                err.strerror += ' Error adding "{}" to the DLL directories.'.format(dll_path)
                raise err

    try:
        ctypes.CDLL('vcruntime140.dll')
        ctypes.CDLL('msvcp140.dll')
        if cuda_version not in ('9.2', '10.0'):
            ctypes.CDLL('vcruntime140_1.dll')
    except OSError:
        print('''Microsoft Visual C++ Redistributable is not installed, this may lead to the DLL load failure.
                 It can be downloaded at https://aka.ms/vs/16/release/vc_redist.x64.exe''')

    dlls = glob.glob(os.path.join(th_dll_path, '*.dll'))
    path_patched = False
    for dll in dlls:
        is_loaded = False
        if with_load_library_flags:
            res = kernel32.LoadLibraryExW(dll, None, 0x00001100)
            last_error = ctypes.get_last_error()
            if res is None and last_error != 126:
                err = ctypes.WinError(last_error)
                err.strerror += ' Error loading "{}" or one of its dependencies.'.format(dll)
                raise err
            elif res is not None:
                is_loaded = True
        if not is_loaded:
            if not path_patched:
                os.environ['PATH'] = ';'.join(dll_paths + [os.environ['PATH']])
                path_patched = True
            res = kernel32.LoadLibraryW(dll)
            if res is None:
                err = ctypes.WinError(ctypes.get_last_error())
                err.strerror += ' Error loading "{}" or one of its dependencies.'.format(dll)
                raise err

    kernel32.SetErrorMode(prev_error_mode)


# See Note [Global dependencies]
def _load_global_deps():
    if platform.system() == 'Windows':
        return

    lib_name = 'libtorch_global_deps' + ('.dylib' if platform.system() == 'Darwin' else '.so')
    here = os.path.abspath(__file__)
    lib_path = os.path.join(os.path.dirname(here), 'lib', lib_name)

    ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)


if (USE_RTLD_GLOBAL_WITH_LIBTORCH or os.getenv('TORCH_USE_RTLD_GLOBAL')) and \
        platform.system() != 'Windows':
    # Do it the hard way.  You might want to load libtorch with RTLD_GLOBAL in a
    # few circumstances:
    #
    #   1. You're in a build environment (e.g., fbcode) where
    #      libtorch_global_deps is not available, but you still need
    #      to get mkl to link in with RTLD_GLOBAL or it will just
    #      not work.
    #
    #   2. You're trying to run PyTorch under UBSAN and you need
    #      to ensure that only one copy of libtorch is loaded, so
    #      vptr checks work properly
    #
    # If you're using this setting, you must verify that all the libraries
    # you load consistently use the same libstdc++, or you may have
    # mysterious segfaults.
    #
    import os as _dl_flags
    if not hasattr(_dl_flags, 'RTLD_GLOBAL') or not hasattr(_dl_flags, 'RTLD_LAZY'):
        try:
            # next try if DLFCN exists
            import DLFCN as _dl_flags  # type: ignore
        except ImportError:
            # as a last attempt, use compile-time constants
            import torch._dl as _dl_flags  # type: ignore
    old_flags = sys.getdlopenflags()
    sys.setdlopenflags(_dl_flags.RTLD_GLOBAL | _dl_flags.RTLD_LAZY)
    from torch._C import *
    sys.setdlopenflags(old_flags)
    del old_flags
    del _dl_flags

else:
    # Easy way.  You want this most of the time, because it will prevent
    # C++ symbols from libtorch clobbering C++ symbols from other
    # libraries, leading to mysterious segfaults.
    #
    # If building in an environment where libtorch_global_deps isn't available
    # like parts of fbsource, but where RTLD_GLOBAL causes segfaults, you will
    # want USE_RTLD_GLOBAL_WITH_LIBTORCH = False and USE_GLOBAL_DEPS = False
    #
    # See Note [Global dependencies]
    if USE_GLOBAL_DEPS:
        _load_global_deps()
    from torch._C import *

# Appease the type checker; ordinarily this binding is inserted by the
# torch._C module initialization code in C
if False:
    import torch._C as _C

__all__ += [name for name in dir(_C)
            if name[0] != '_' and
            not name.endswith('Base')]

################################################################################
# Define basic utilities
################################################################################


def typename(o):
    if isinstance(o, torch.Tensor):
        return o.type()

    module = ''
    class_name = ''
    if hasattr(o, '__module__') and o.__module__ != 'builtins' \
            and o.__module__ != '__builtin__' and o.__module__ is not None:
        module = o.__module__ + '.'

    if hasattr(o, '__qualname__'):
        class_name = o.__qualname__
    elif hasattr(o, '__name__'):
        class_name = o.__name__
    else:
        class_name = o.__class__.__name__

    return module + class_name


def is_tensor(obj):
    r"""Returns True if `obj` is a PyTorch tensor.

    Note that this function is simply doing ``isinstance(obj, Tensor)``.
    Using that ``isinstance`` check is better for typechecking with mypy,
    and more explicit - so it's recommended to use that instead of
    ``is_tensor``.

    Args:
        obj (Object): Object to test
    """
    return isinstance(obj, torch.Tensor)


def is_storage(obj):
    r"""Returns True if `obj` is a PyTorch storage object.

    Args:
        obj (Object): Object to test
    """
    return type(obj) in _storage_classes


def set_default_tensor_type(t):
    r"""Sets the default ``torch.Tensor`` type to floating point tensor type
    ``t``. This type will also be used as default floating point type for
    type inference in :func:`torch.tensor`.

    The default floating point tensor type is initially ``torch.FloatTensor``.

    Args:
        t (type or string): the floating point tensor type or its name

    Example::

        >>> torch.tensor([1.2, 3]).dtype    # initial default for floating point is torch.float32
        torch.float32
        >>> torch.set_default_tensor_type(torch.DoubleTensor)
        >>> torch.tensor([1.2, 3]).dtype    # a new floating point tensor
        torch.float64

    """
    if isinstance(t, _string_classes):
        t = _import_dotted_name(t)
    _C._set_default_tensor_type(t)


def set_default_dtype(d):
    r"""Sets the default floating point dtype to :attr:`d`.
    This dtype is:
    1. The inferred dtype for python floats in :func:`torch.tensor`.
    2. Used to infer dtype for python complex numbers. The default complex dtype is set to
       ``torch.complex128`` if default floating point dtype is ``torch.float64``,
       otherwise it's set to ``torch.complex64``

    The default floating point dtype is initially ``torch.float32``.

    Args:
        d (:class:`torch.dtype`): the floating point dtype to make the default

    Example::
        >>> # initial default for floating point is torch.float32
        >>> torch.tensor([1.2, 3]).dtype
        torch.float32
        >>> # initial default for floating point is torch.complex64
        >>> torch.tensor([1.2, 3j]).dtype
        torch.complex64
        >>> torch.set_default_dtype(torch.float64)
        >>> torch.tensor([1.2, 3]).dtype    # a new floating point tensor
        torch.float64
        >>> torch.tensor([1.2, 3j]).dtype   # a new complex tensor
        torch.complex128

    """
    _C._set_default_dtype(d)

def _set_deterministic(d):
    r"""Sets a global flag to force all operations to use a deterministic
    implementation if available. If an operation that does not have a
    deterministic implementation is called while this setting is True, the
    operation will throw a RuntimeError.

    Note that deterministic operations tend to have worse performance than
    non-deterministic operations.

    Args:
        d (:class:`bool`): If True, force operations to be deterministic.
                           If False, allow non-deterministic operations.

    .. warning::
        This feature is experimental and not complete. The above docstring
        represents what the future behavior is intended to be. Right now,
        `_set_deterministic` will only affect `torch.bmm` and convolution
        operators.
    """
    _C._set_deterministic(d)

def _is_deterministic():
    r"""Returns True if the global deterministic flag is turned on and
    operations are being forced to use a deterministic implementation.

    .. warning::
        This feature is experimental and not complete. The above docstring
        represents what the future behavior is intended to be. Right now,
        the global deterministic flag will only affect `torch.bmm` and
        convolution operators.
    """
    return _C._get_deterministic()

# If you edit these imports, please update torch/__init__.py.in as well
from .random import set_rng_state, get_rng_state, manual_seed, initial_seed, seed
from .serialization import save, load
from ._tensor_str import set_printoptions

################################################################################
# Define Storage and Tensor classes
################################################################################

from .tensor import Tensor
from .storage import _StorageBase


class DoubleStorage(_C.DoubleStorageBase, _StorageBase):
    pass


class FloatStorage(_C.FloatStorageBase, _StorageBase):
    pass


class HalfStorage(_C.HalfStorageBase, _StorageBase):
    pass


class LongStorage(_C.LongStorageBase, _StorageBase):
    pass


class IntStorage(_C.IntStorageBase, _StorageBase):
    pass


class ShortStorage(_C.ShortStorageBase, _StorageBase):
    pass


class CharStorage(_C.CharStorageBase, _StorageBase):
    pass


class ByteStorage(_C.ByteStorageBase, _StorageBase):
    pass


class BoolStorage(_C.BoolStorageBase, _StorageBase):
    pass


class BFloat16Storage(_C.BFloat16StorageBase, _StorageBase):
    pass

class ComplexDoubleStorage(_C.ComplexDoubleStorageBase, _StorageBase):
    pass

class ComplexFloatStorage(_C.ComplexFloatStorageBase, _StorageBase):
    pass

class QUInt8Storage(_C.QUInt8StorageBase, _StorageBase):
    pass

class QInt8Storage(_C.QInt8StorageBase, _StorageBase):
    pass

class QInt32Storage(_C.QInt32StorageBase, _StorageBase):
    pass


_storage_classes = {
    DoubleStorage, FloatStorage, LongStorage, IntStorage, ShortStorage,
    CharStorage, ByteStorage, HalfStorage, BoolStorage, QUInt8Storage, QInt8Storage,
    QInt32Storage, BFloat16Storage, ComplexFloatStorage, ComplexDoubleStorage
}

# The _tensor_classes set is initialized by the call to _C._initialize_tensor_type_bindings()
_tensor_classes: Set[Type] = set()


################################################################################
# Initialize extension
################################################################################

def manager_path():
    if platform.system() == 'Windows':
        return b""
    path = get_file_path('torch', 'bin', 'torch_shm_manager')
    prepare_multiprocessing_environment(get_file_path('torch'))
    if not os.path.exists(path):
        raise RuntimeError("Unable to find torch_shm_manager at " + path)
    return path.encode('utf-8')


# Shared memory manager needs to know the exact location of manager executable
_C._initExtension(manager_path())
del manager_path

# Appease the type checker: it can't deal with direct setting of globals().
# Note that we will see "too many" functions when reexporting this way; there
# is not a good way to fix this problem.  Perhaps, try to redesign VariableFunctions
# so that this import is good enough
if False:
    from torch._C._VariableFunctions import *

for name in dir(_C._VariableFunctions):
    if name.startswith('__'):
        continue
    globals()[name] = getattr(_C._VariableFunctions, name)
    __all__.append(name)

################################################################################
# Import interface functions defined in Python
################################################################################

# needs to be after the above ATen bindings so we can overwrite from Python side
from .functional import *


################################################################################
# Remove unnecessary members
################################################################################

del DoubleStorageBase
del FloatStorageBase
del LongStorageBase
del IntStorageBase
del ShortStorageBase
del CharStorageBase
del ByteStorageBase
del BoolStorageBase
del QUInt8StorageBase
del BFloat16StorageBase
del ComplexDoubleStorageBase
del ComplexFloatStorageBase

################################################################################
# Import most common subpackages
################################################################################

import torch.cuda
import torch.autograd
from torch.autograd import no_grad, enable_grad, set_grad_enabled
import torch.futures
import torch.nn
import torch.nn.intrinsic
import torch.nn.quantized
import torch.optim
import torch.multiprocessing
import torch.sparse
import torch.utils.backcompat
import torch.onnx
import torch.jit
import torch.hub
import torch.random
import torch.distributions
import torch.testing
import torch.backends.cuda
import torch.backends.mkl
import torch.backends.mkldnn
import torch.backends.openmp
import torch.backends.quantized
import torch.quantization
import torch.utils.data
import torch.__config__
import torch.__future__

_C._init_names(list(torch._storage_classes))

# attach docstrings to torch and tensor functions
from . import _torch_docs, _tensor_docs, _storage_docs
del _torch_docs, _tensor_docs, _storage_docs


def compiled_with_cxx11_abi():
    r"""Returns whether PyTorch was built with _GLIBCXX_USE_CXX11_ABI=1"""
    return _C._GLIBCXX_USE_CXX11_ABI


# Import the ops "namespace"
from torch._ops import ops
from torch._classes import classes

# Import the quasi random sampler
import torch.quasirandom

# If you are seeing this, it means that this call site was not checked if
# the memory format could be preserved, and it was switched to old default
# behaviour of contiguous
legacy_contiguous_format = contiguous_format

# Register fork handler to initialize OpenMP in child processes (see gh-28389)
from torch.multiprocessing._atfork import register_after_fork
register_after_fork(torch.get_num_threads)
del register_after_fork

# Import tools that require fully imported torch (for applying
# torch.jit.script as a decorator, for instance):
from ._lobpcg import lobpcg

# These were previously defined in native_functions.yaml and appeared on the
# `torch` namespace, but we moved them to c10 dispatch to facilitate custom
# class usage. We add these lines here to preserve backward compatbility.
quantized_lstm = torch.ops.aten.quantized_lstm
quantized_gru = torch.ops.aten.quantized_gru
