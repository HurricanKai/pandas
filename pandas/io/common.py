"""Common IO api utilities"""

import bz2
from collections import abc
import dataclasses
import gzip
from io import BufferedIOBase, BytesIO, RawIOBase, TextIOWrapper
import mmap
import os
import pathlib
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    AnyStr,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)
from urllib.parse import (
    urljoin,
    urlparse as parse_url,
    uses_netloc,
    uses_params,
    uses_relative,
)
import warnings
import zipfile

from pandas._typing import (
    Buffer,
    CompressionDict,
    CompressionOptions,
    EncodingVar,
    FileOrBuffer,
    FilePathOrBuffer,
    ModeVar,
    StorageOptions,
)
from pandas.compat import get_lzma_file, import_lzma
from pandas.compat._optional import import_optional_dependency

from pandas.core.dtypes.common import is_file_like

lzma = import_lzma()


_VALID_URLS = set(uses_relative + uses_netloc + uses_params)
_VALID_URLS.discard("")


if TYPE_CHECKING:
    from io import IOBase


@dataclasses.dataclass
class IOArgs(Generic[ModeVar, EncodingVar]):
    """
    Return value of io/common.py:get_filepath_or_buffer.

    This is used to easily close created fsspec objects.

    Note (copy&past from io/parsers):
    filepath_or_buffer can be Union[FilePathOrBuffer, s3fs.S3File, gcsfs.GCSFile]
    though mypy handling of conditional imports is difficult.
    See https://github.com/python/mypy/issues/1297
    """

    filepath_or_buffer: FileOrBuffer
    encoding: EncodingVar
    mode: Union[ModeVar, str]
    compression: CompressionDict
    should_close: bool = False

    def close(self) -> None:
        """
        Close the buffer if it was created by get_filepath_or_buffer.
        """
        if self.should_close:
            assert not isinstance(self.filepath_or_buffer, str)
            try:
                self.filepath_or_buffer.close()
            except (OSError, ValueError):
                pass
        self.should_close = False


@dataclasses.dataclass
class IOHandles:
    """
    Return value of io/common.py:get_handle

    This is used to easily close created buffers and to handle corner cases when
    TextIOWrapper is inserted.

    handle: The file handle to be used.
    created_handles: All file handles that are created by get_handle
    is_wrapped: Whether a TextIOWrapper needs to be detached.
    """

    handle: Buffer
    created_handles: List[Buffer] = dataclasses.field(default_factory=list)
    is_wrapped: bool = False

    def close(self) -> None:
        """
        Close all created buffers.

        Note: If a TextIOWrapper was inserted, it is flushed and detached to
        avoid closing the potentially user-created buffer.
        """
        if self.is_wrapped:
            assert isinstance(self.handle, TextIOWrapper)
            self.handle.flush()
            self.handle.detach()
            self.created_handles.remove(self.handle)
        try:
            for handle in self.created_handles:
                handle.close()
        except (OSError, ValueError):
            pass
        self.created_handles = []
        self.is_wrapped = False


def is_url(url) -> bool:
    """
    Check to see if a URL has a valid protocol.

    Parameters
    ----------
    url : str or unicode

    Returns
    -------
    isurl : bool
        If `url` has a valid protocol return True otherwise False.
    """
    if not isinstance(url, str):
        return False
    return parse_url(url).scheme in _VALID_URLS


def _expand_user(filepath_or_buffer: FileOrBuffer[AnyStr]) -> FileOrBuffer[AnyStr]:
    """
    Return the argument with an initial component of ~ or ~user
    replaced by that user's home directory.

    Parameters
    ----------
    filepath_or_buffer : object to be converted if possible

    Returns
    -------
    expanded_filepath_or_buffer : an expanded filepath or the
                                  input if not expandable
    """
    if isinstance(filepath_or_buffer, str):
        return os.path.expanduser(filepath_or_buffer)
    return filepath_or_buffer


def validate_header_arg(header) -> None:
    if isinstance(header, bool):
        raise TypeError(
            "Passing a bool to header is invalid. Use header=None for no header or "
            "header=int or list-like of ints to specify "
            "the row(s) making up the column names"
        )


def stringify_path(
    filepath_or_buffer: FilePathOrBuffer[AnyStr],
) -> FileOrBuffer[AnyStr]:
    """
    Attempt to convert a path-like object to a string.

    Parameters
    ----------
    filepath_or_buffer : object to be converted

    Returns
    -------
    str_filepath_or_buffer : maybe a string version of the object

    Notes
    -----
    Objects supporting the fspath protocol (python 3.6+) are coerced
    according to its __fspath__ method.

    For backwards compatibility with older pythons, pathlib.Path and
    py.path objects are specially coerced.

    Any other object is passed through unchanged, which includes bytes,
    strings, buffers, or anything else that's not even path-like.
    """
    if hasattr(filepath_or_buffer, "__fspath__"):
        # https://github.com/python/mypy/issues/1424
        # error: Item "str" of "Union[str, Path, IO[str]]" has no attribute
        # "__fspath__"  [union-attr]
        # error: Item "IO[str]" of "Union[str, Path, IO[str]]" has no attribute
        # "__fspath__"  [union-attr]
        # error: Item "str" of "Union[str, Path, IO[bytes]]" has no attribute
        # "__fspath__"  [union-attr]
        # error: Item "IO[bytes]" of "Union[str, Path, IO[bytes]]" has no
        # attribute "__fspath__"  [union-attr]
        filepath_or_buffer = filepath_or_buffer.__fspath__()  # type: ignore[union-attr]
    elif isinstance(filepath_or_buffer, pathlib.Path):
        filepath_or_buffer = str(filepath_or_buffer)
    return _expand_user(filepath_or_buffer)


def urlopen(*args, **kwargs):
    """
    Lazy-import wrapper for stdlib urlopen, as that imports a big chunk of
    the stdlib.
    """
    import urllib.request

    return urllib.request.urlopen(*args, **kwargs)


def is_fsspec_url(url: FilePathOrBuffer) -> bool:
    """
    Returns true if the given URL looks like
    something fsspec can handle
    """
    return (
        isinstance(url, str)
        and "://" in url
        and not url.startswith(("http://", "https://"))
    )


# https://github.com/python/mypy/issues/8708
# error: Incompatible default for argument "encoding" (default has type "None",
# argument has type "str")
# error: Incompatible default for argument "mode" (default has type "None",
# argument has type "str")
def get_filepath_or_buffer(
    filepath_or_buffer: FilePathOrBuffer,
    encoding: EncodingVar = None,  # type: ignore[assignment]
    compression: CompressionOptions = None,
    mode: ModeVar = None,  # type: ignore[assignment]
    storage_options: StorageOptions = None,
) -> IOArgs[ModeVar, EncodingVar]:
    """
    If the filepath_or_buffer is a url, translate and return the buffer.
    Otherwise passthrough.

    Parameters
    ----------
    filepath_or_buffer : a url, filepath (str, py.path.local or pathlib.Path),
                         or buffer
    compression : {{'gzip', 'bz2', 'zip', 'xz', None}}, optional
    encoding : the encoding to use to decode bytes, default is 'utf-8'
    mode : str, optional

    storage_options : dict, optional
        Extra options that make sense for a particular storage connection, e.g.
        host, port, username, password, etc., if using a URL that will
        be parsed by ``fsspec``, e.g., starting "s3://", "gcs://". An error
        will be raised if providing this argument with a local path or
        a file-like buffer. See the fsspec and backend storage implementation
        docs for the set of allowed keys and values

        .. versionadded:: 1.2.0

    ..versionchange:: 1.2.0

      Returns the dataclass IOArgs.
    """
    filepath_or_buffer = stringify_path(filepath_or_buffer)

    # handle compression dict
    compression_method, compression = get_compression_method(compression)
    compression_method = infer_compression(filepath_or_buffer, compression_method)

    # GH21227 internal compression is not used for non-binary handles.
    if (
        compression_method
        and hasattr(filepath_or_buffer, "write")
        and mode
        and "b" not in mode
    ):
        warnings.warn(
            "compression has no effect when passing a non-binary object as input.",
            RuntimeWarning,
            stacklevel=2,
        )
        compression_method = None

    compression = dict(compression, method=compression_method)

    # uniform encoding names
    if encoding is not None:
        encoding = encoding.replace("_", "-").lower()

    # bz2 and xz do not write the byte order mark for utf-16 and utf-32
    # print a warning when writing such files
    if (
        mode
        and "w" in mode
        and compression_method in ["bz2", "xz"]
        and encoding in ["utf-16", "utf-32"]
    ):
        warnings.warn(
            f"{compression} will not write the byte order mark for {encoding}",
            UnicodeWarning,
        )

    # Use binary mode when converting path-like objects to file-like objects (fsspec)
    # except when text mode is explicitly requested. The original mode is returned if
    # fsspec is not used.
    fsspec_mode = mode or "rb"
    if "t" not in fsspec_mode and "b" not in fsspec_mode:
        fsspec_mode += "b"

    if isinstance(filepath_or_buffer, str) and is_url(filepath_or_buffer):
        # TODO: fsspec can also handle HTTP via requests, but leaving this unchanged
        if storage_options:
            raise ValueError(
                "storage_options passed with file object or non-fsspec file path"
            )
        req = urlopen(filepath_or_buffer)
        content_encoding = req.headers.get("Content-Encoding", None)
        if content_encoding == "gzip":
            # Override compression based on Content-Encoding header
            compression = {"method": "gzip"}
        reader = BytesIO(req.read())
        req.close()
        return IOArgs(
            filepath_or_buffer=reader,
            encoding=encoding,
            compression=compression,
            should_close=True,
            mode=fsspec_mode,
        )

    if is_fsspec_url(filepath_or_buffer):
        assert isinstance(
            filepath_or_buffer, str
        )  # just to appease mypy for this branch
        # two special-case s3-like protocols; these have special meaning in Hadoop,
        # but are equivalent to just "s3" from fsspec's point of view
        # cc #11071
        if filepath_or_buffer.startswith("s3a://"):
            filepath_or_buffer = filepath_or_buffer.replace("s3a://", "s3://")
        if filepath_or_buffer.startswith("s3n://"):
            filepath_or_buffer = filepath_or_buffer.replace("s3n://", "s3://")
        fsspec = import_optional_dependency("fsspec")

        # If botocore is installed we fallback to reading with anon=True
        # to allow reads from public buckets
        err_types_to_retry_with_anon: List[Any] = []
        try:
            import_optional_dependency("botocore")
            from botocore.exceptions import ClientError, NoCredentialsError

            err_types_to_retry_with_anon = [
                ClientError,
                NoCredentialsError,
                PermissionError,
            ]
        except ImportError:
            pass

        try:
            file_obj = fsspec.open(
                filepath_or_buffer, mode=fsspec_mode, **(storage_options or {})
            ).open()
        # GH 34626 Reads from Public Buckets without Credentials needs anon=True
        except tuple(err_types_to_retry_with_anon):
            if storage_options is None:
                storage_options = {"anon": True}
            else:
                # don't mutate user input.
                storage_options = dict(storage_options)
                storage_options["anon"] = True
            file_obj = fsspec.open(
                filepath_or_buffer, mode=fsspec_mode, **(storage_options or {})
            ).open()

        return IOArgs(
            filepath_or_buffer=file_obj,
            encoding=encoding,
            compression=compression,
            should_close=True,
            mode=fsspec_mode,
        )
    elif storage_options:
        raise ValueError(
            "storage_options passed with file object or non-fsspec file path"
        )

    if isinstance(filepath_or_buffer, (str, bytes, mmap.mmap)):
        return IOArgs(
            filepath_or_buffer=_expand_user(filepath_or_buffer),
            encoding=encoding,
            compression=compression,
            should_close=False,
            mode=mode,
        )

    if not is_file_like(filepath_or_buffer):
        msg = f"Invalid file path or buffer object type: {type(filepath_or_buffer)}"
        raise ValueError(msg)

    return IOArgs(
        filepath_or_buffer=filepath_or_buffer,
        encoding=encoding,
        compression=compression,
        should_close=False,
        mode=mode,
    )


def file_path_to_url(path: str) -> str:
    """
    converts an absolute native path to a FILE URL.

    Parameters
    ----------
    path : a path in native format

    Returns
    -------
    a valid FILE URL
    """
    # lazify expensive import (~30ms)
    from urllib.request import pathname2url

    return urljoin("file:", pathname2url(path))


_compression_to_extension = {"gzip": ".gz", "bz2": ".bz2", "zip": ".zip", "xz": ".xz"}


def get_compression_method(
    compression: CompressionOptions,
) -> Tuple[Optional[str], CompressionDict]:
    """
    Simplifies a compression argument to a compression method string and
    a mapping containing additional arguments.

    Parameters
    ----------
    compression : str or mapping
        If string, specifies the compression method. If mapping, value at key
        'method' specifies compression method.

    Returns
    -------
    tuple of ({compression method}, Optional[str]
              {compression arguments}, Dict[str, Any])

    Raises
    ------
    ValueError on mapping missing 'method' key
    """
    compression_method: Optional[str]
    if isinstance(compression, Mapping):
        compression_args = dict(compression)
        try:
            compression_method = compression_args.pop("method")
        except KeyError as err:
            raise ValueError("If mapping, compression must have key 'method'") from err
    else:
        compression_args = {}
        compression_method = compression
    return compression_method, compression_args


def infer_compression(
    filepath_or_buffer: FilePathOrBuffer, compression: Optional[str]
) -> Optional[str]:
    """
    Get the compression method for filepath_or_buffer. If compression='infer',
    the inferred compression method is returned. Otherwise, the input
    compression method is returned unchanged, unless it's invalid, in which
    case an error is raised.

    Parameters
    ----------
    filepath_or_buffer : str or file handle
        File path or object.
    compression : {'infer', 'gzip', 'bz2', 'zip', 'xz', None}
        If 'infer' and `filepath_or_buffer` is path-like, then detect
        compression from the following extensions: '.gz', '.bz2', '.zip',
        or '.xz' (otherwise no compression).

    Returns
    -------
    string or None

    Raises
    ------
    ValueError on invalid compression specified.
    """
    # No compression has been explicitly specified
    if compression is None:
        return None

    # Infer compression
    if compression == "infer":
        # Convert all path types (e.g. pathlib.Path) to strings
        filepath_or_buffer = stringify_path(filepath_or_buffer)
        if not isinstance(filepath_or_buffer, str):
            # Cannot infer compression of a buffer, assume no compression
            return None

        # Infer compression from the filename/URL extension
        for compression, extension in _compression_to_extension.items():
            if filepath_or_buffer.lower().endswith(extension):
                return compression
        return None

    # Compression has been specified. Check that it's valid
    if compression in _compression_to_extension:
        return compression

    msg = f"Unrecognized compression type: {compression}"
    valid = ["infer", None] + sorted(_compression_to_extension)
    msg += f"\nValid compression types are {valid}"
    raise ValueError(msg)


def get_handle(
    path_or_buf: FilePathOrBuffer,
    mode: str,
    encoding: Optional[str] = None,
    compression: CompressionOptions = None,
    memory_map: bool = False,
    is_text: bool = True,
    errors: Optional[str] = None,
) -> IOHandles:
    """
    Get file handle for given path/buffer and mode.

    Parameters
    ----------
    path_or_buf : str or file handle
        File path or object.
    mode : str
        Mode to open path_or_buf with.
    encoding : str or None
        Encoding to use.
    compression : str or dict, default None
        If string, specifies compression mode. If dict, value at key 'method'
        specifies compression mode. Compression mode must be one of {'infer',
        'gzip', 'bz2', 'zip', 'xz', None}. If compression mode is 'infer'
        and `filepath_or_buffer` is path-like, then detect compression from
        the following extensions: '.gz', '.bz2', '.zip', or '.xz' (otherwise
        no compression). If dict and compression mode is one of
        {'zip', 'gzip', 'bz2'}, or inferred as one of the above,
        other entries passed as additional compression options.

        .. versionchanged:: 1.0.0

           May now be a dict with key 'method' as compression mode
           and other keys as compression options if compression
           mode is 'zip'.

        .. versionchanged:: 1.1.0

           Passing compression options as keys in dict is now
           supported for compression modes 'gzip' and 'bz2' as well as 'zip'.

    memory_map : boolean, default False
        See parsers._parser_params for more information.
    is_text : boolean, default True
        Whether the type of the content passed to the file/buffer is string or
        bytes. This is not the same as `"b" not in mode`. If a string content is
        passed to a binary file/buffer, a wrapper is inserted.
    errors : str, default 'strict'
        Specifies how encoding and decoding errors are to be handled.
        See the errors argument for :func:`open` for a full list
        of options.

    .. versionchanged:: 1.2.0

    Returns the dataclass IOHandles
    """
    need_text_wrapping: Tuple[Type["IOBase"], ...]
    try:
        from s3fs import S3File

        need_text_wrapping = (BufferedIOBase, RawIOBase, S3File)
    except ImportError:
        need_text_wrapping = (BufferedIOBase, RawIOBase)
    # fsspec is an optional dependency. If it is available, add its file-object
    # class to the list of classes that need text wrapping. If fsspec is too old and is
    # needed, get_filepath_or_buffer would already have thrown an exception.
    try:
        from fsspec.spec import AbstractFileSystem

        need_text_wrapping = (*need_text_wrapping, AbstractFileSystem)
    except ImportError:
        pass

    handles: List[Buffer] = list()

    # Windows does not default to utf-8. Set to utf-8 for a consistent behavior
    if encoding is None:
        encoding = "utf-8"

    # Convert pathlib.Path/py.path.local or string
    path_or_buf = stringify_path(path_or_buf)
    is_path = isinstance(path_or_buf, str)
    f = path_or_buf

    compression, compression_args = get_compression_method(compression)
    if is_path:
        compression = infer_compression(path_or_buf, compression)

    if compression:

        # GZ Compression
        if compression == "gzip":
            if is_path:
                assert isinstance(path_or_buf, str)
                f = gzip.GzipFile(filename=path_or_buf, mode=mode, **compression_args)
            else:
                f = gzip.GzipFile(
                    fileobj=path_or_buf,  # type: ignore[arg-type]
                    mode=mode,
                    **compression_args,
                )

        # BZ Compression
        elif compression == "bz2":
            f = bz2.BZ2File(
                path_or_buf, mode=mode, **compression_args  # type: ignore[arg-type]
            )

        # ZIP Compression
        elif compression == "zip":
            f = _BytesZipFile(path_or_buf, mode, **compression_args)
            if f.mode == "r":
                handles.append(f)
                zip_names = f.namelist()
                if len(zip_names) == 1:
                    f = f.open(zip_names.pop())
                elif len(zip_names) == 0:
                    raise ValueError(f"Zero files found in ZIP file {path_or_buf}")
                else:
                    raise ValueError(
                        "Multiple files found in ZIP file. "
                        f"Only one file per ZIP: {zip_names}"
                    )

        # XZ Compression
        elif compression == "xz":
            f = get_lzma_file(lzma)(path_or_buf, mode)

        # Unrecognized Compression
        else:
            msg = f"Unrecognized compression type: {compression}"
            raise ValueError(msg)

        assert not isinstance(f, str)
        handles.append(f)

    elif is_path:
        # Check whether the filename is to be opened in binary mode.
        # Binary mode does not support 'encoding' and 'newline'.
        is_binary_mode = "b" in mode
        assert isinstance(path_or_buf, str)
        if encoding and not is_binary_mode:
            # Encoding
            f = open(path_or_buf, mode, encoding=encoding, errors=errors, newline="")
        else:
            # Binary mode
            f = open(path_or_buf, mode)
        handles.append(f)

    # Convert BytesIO or file objects passed with an encoding
    is_wrapped = False
    if is_text and (
        compression
        or isinstance(f, need_text_wrapping)
        or "b" in getattr(f, "mode", "")
    ):
        f = TextIOWrapper(
            f, encoding=encoding, errors=errors, newline=""  # type: ignore[arg-type]
        )
        handles.append(f)
        # do not mark as wrapped when the user provided a string
        is_wrapped = not is_path

    if memory_map and hasattr(f, "fileno"):
        assert not isinstance(f, str)
        try:
            wrapped = cast(mmap.mmap, _MMapWrapper(f))  # type: ignore[arg-type]
            f.close()
            handles.remove(f)
            handles.append(wrapped)
            f = wrapped
        except Exception:
            # we catch any errors that may have occurred
            # because that is consistent with the lower-level
            # functionality of the C engine (pd.read_csv), so
            # leave the file handler as is then
            pass

    handles.reverse()  # close the most recently added buffer first
    assert not isinstance(f, str)
    return IOHandles(
        handle=f,
        created_handles=handles,
        is_wrapped=is_wrapped,
    )


# error: Definition of "__exit__" in base class "ZipFile" is incompatible with
# definition in base class "BytesIO"  [misc]
# error: Definition of "__enter__" in base class "ZipFile" is incompatible with
# definition in base class "BytesIO"  [misc]
# error: Definition of "__enter__" in base class "ZipFile" is incompatible with
# definition in base class "BinaryIO"  [misc]
# error: Definition of "__enter__" in base class "ZipFile" is incompatible with
# definition in base class "IO"  [misc]
# error: Definition of "read" in base class "ZipFile" is incompatible with
# definition in base class "BytesIO"  [misc]
# error: Definition of "read" in base class "ZipFile" is incompatible with
# definition in base class "IO"  [misc]
class _BytesZipFile(zipfile.ZipFile, BytesIO):  # type: ignore[misc]
    """
    Wrapper for standard library class ZipFile and allow the returned file-like
    handle to accept byte strings via `write` method.

    BytesIO provides attributes of file-like object and ZipFile.writestr writes
    bytes strings into a member of the archive.
    """

    # GH 17778
    def __init__(
        self,
        file: FilePathOrBuffer,
        mode: str,
        archive_name: Optional[str] = None,
        **kwargs,
    ):
        if mode in ["wb", "rb"]:
            mode = mode.replace("b", "")
        self.archive_name = archive_name
        kwargs_zip: Dict[str, Any] = {"compression": zipfile.ZIP_DEFLATED}
        kwargs_zip.update(kwargs)
        super().__init__(file, mode, **kwargs_zip)  # type: ignore[arg-type]

    def write(self, data):
        # ZipFile needs a non-empty string
        archive_name = self.archive_name or self.filename or "zip"
        super().writestr(archive_name, data)

    @property
    def closed(self):
        return self.fp is None


class _MMapWrapper(abc.Iterator):
    """
    Wrapper for the Python's mmap class so that it can be properly read in
    by Python's csv.reader class.

    Parameters
    ----------
    f : file object
        File object to be mapped onto memory. Must support the 'fileno'
        method or have an equivalent attribute

    """

    def __init__(self, f: IO):
        self.mmap = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

    def __getattr__(self, name: str):
        return getattr(self.mmap, name)

    def __iter__(self) -> "_MMapWrapper":
        return self

    def __next__(self) -> str:
        newbytes = self.mmap.readline()

        # readline returns bytes, not str, but Python's CSV reader
        # expects str, so convert the output to str before continuing
        newline = newbytes.decode("utf-8")

        # mmap doesn't raise if reading past the allocated
        # data but instead returns an empty string, so raise
        # if that is returned
        if newline == "":
            raise StopIteration
        return newline
