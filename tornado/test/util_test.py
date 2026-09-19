import datetime
import gzip
import re
import sys
import textwrap
import zlib
from typing import Any, cast

import tornado
from tornado.escape import utf8
from tornado.util import (
    ArgReplacer,
    Configurable,
    GzipDecompressor,
    exec_in,
    import_object,
    raise_exc_info,
    re_unescape,
    timedelta_to_seconds,
)


import unittest

from tornado.test.util import TestCase


class RaiseExcInfoTest(TestCase):
    def test_two_arg_exception(self):
        # This test would fail on python 3 if raise_exc_info were simply
        # a three-argument raise statement, because TwoArgException
        # doesn't have a "copy constructor"
        class TwoArgException(Exception):
            def __init__(self, a, b):
                super().__init__()
                self.a, self.b = a, b

        try:
            raise TwoArgException(1, 2)
        except TwoArgException:
            exc_info = sys.exc_info()
        try:
            raise_exc_info(exc_info)
            self.fail("didn't get expected exception")
        except TwoArgException as e:
            self.assertIs(e, exc_info[1])


class TestConfigurable(Configurable):
    @classmethod
    def configurable_base(cls):
        return TestConfigurable

    @classmethod
    def configurable_default(cls):
        return TestConfig1


class TestConfig1(TestConfigurable):
    def initialize(self, pos_arg=None, a=None):
        self.a = a
        self.pos_arg = pos_arg


class TestConfig2(TestConfigurable):
    def initialize(self, pos_arg=None, b=None):
        self.b = b
        self.pos_arg = pos_arg


class TestConfig3(TestConfigurable):
    # TestConfig3 is a configuration option that is itself configurable.
    @classmethod
    def configurable_base(cls):
        return TestConfig3

    @classmethod
    def configurable_default(cls):
        return TestConfig3A


class TestConfig3A(TestConfig3):
    def initialize(self, a=None):
        self.a = a


class TestConfig3B(TestConfig3):
    def initialize(self, b=None):
        self.b = b


class ConfigurableTest(TestCase):
    def setUp(self):
        self.saved = TestConfigurable._save_configuration()
        self.saved3 = TestConfig3._save_configuration()

    def tearDown(self):
        TestConfigurable._restore_configuration(self.saved)
        TestConfig3._restore_configuration(self.saved3)

    def checkSubclasses(self):
        # no matter how the class is configured, it should always be
        # possible to instantiate the subclasses directly
        self.assertIsInstance(TestConfig1(), TestConfig1)
        self.assertIsInstance(TestConfig2(), TestConfig2)

        obj = TestConfig1(a=1)
        self.assertEqual(obj.a, 1)
        obj2 = TestConfig2(b=2)
        self.assertEqual(obj2.b, 2)

    def test_default(self):
        # In these tests we combine a typing.cast to satisfy mypy with
        # a runtime type-assertion. Without the cast, mypy would only
        # let us access attributes of the base class.
        obj = cast(TestConfig1, TestConfigurable())
        self.assertIsInstance(obj, TestConfig1)
        self.assertIsNone(obj.a)

        obj = cast(TestConfig1, TestConfigurable(a=1))
        self.assertIsInstance(obj, TestConfig1)
        self.assertEqual(obj.a, 1)

        self.checkSubclasses()

    def test_config_class(self):
        TestConfigurable.configure(TestConfig2)
        obj = cast(TestConfig2, TestConfigurable())
        self.assertIsInstance(obj, TestConfig2)
        self.assertIsNone(obj.b)

        obj = cast(TestConfig2, TestConfigurable(b=2))
        self.assertIsInstance(obj, TestConfig2)
        self.assertEqual(obj.b, 2)

        self.checkSubclasses()

    def test_config_str(self):
        TestConfigurable.configure("tornado.test.util_test.TestConfig2")
        obj = cast(TestConfig2, TestConfigurable())
        self.assertIsInstance(obj, TestConfig2)
        self.assertIsNone(obj.b)

        obj = cast(TestConfig2, TestConfigurable(b=2))
        self.assertIsInstance(obj, TestConfig2)
        self.assertEqual(obj.b, 2)

        self.checkSubclasses()

    def test_config_args(self):
        TestConfigurable.configure(None, a=3)
        obj = cast(TestConfig1, TestConfigurable())
        self.assertIsInstance(obj, TestConfig1)
        self.assertEqual(obj.a, 3)

        obj = cast(TestConfig1, TestConfigurable(42, a=4))
        self.assertIsInstance(obj, TestConfig1)
        self.assertEqual(obj.a, 4)
        self.assertEqual(obj.pos_arg, 42)

        self.checkSubclasses()
        # args bound in configure don't apply when using the subclass directly
        obj = TestConfig1()
        self.assertIsNone(obj.a)

    def test_config_class_args(self):
        TestConfigurable.configure(TestConfig2, b=5)
        obj = cast(TestConfig2, TestConfigurable())
        self.assertIsInstance(obj, TestConfig2)
        self.assertEqual(obj.b, 5)

        obj = cast(TestConfig2, TestConfigurable(42, b=6))
        self.assertIsInstance(obj, TestConfig2)
        self.assertEqual(obj.b, 6)
        self.assertEqual(obj.pos_arg, 42)

        self.checkSubclasses()
        # args bound in configure don't apply when using the subclass directly
        obj = TestConfig2()
        self.assertIsNone(obj.b)

    def test_config_multi_level(self):
        TestConfigurable.configure(TestConfig3, a=1)
        obj = cast(TestConfig3A, TestConfigurable())
        self.assertIsInstance(obj, TestConfig3A)
        self.assertEqual(obj.a, 1)

        TestConfigurable.configure(TestConfig3)
        TestConfig3.configure(TestConfig3B, b=2)
        obj2 = cast(TestConfig3B, TestConfigurable())
        self.assertIsInstance(obj2, TestConfig3B)
        self.assertEqual(obj2.b, 2)

    def test_config_inner_level(self):
        # The inner level can be used even when the outer level
        # doesn't point to it.
        obj = TestConfig3()
        self.assertIsInstance(obj, TestConfig3A)

        TestConfig3.configure(TestConfig3B)
        obj = TestConfig3()
        self.assertIsInstance(obj, TestConfig3B)

        # Configuring the base doesn't configure the inner.
        obj2 = TestConfigurable()
        self.assertIsInstance(obj2, TestConfig1)
        TestConfigurable.configure(TestConfig2)

        obj3 = TestConfigurable()
        self.assertIsInstance(obj3, TestConfig2)

        obj = TestConfig3()
        self.assertIsInstance(obj, TestConfig3B)


class UnicodeLiteralTest(TestCase):
    def test_unicode_escapes(self):
        self.assertEqual(utf8("\u00e9"), b"\xc3\xa9")


class ExecInTest(TestCase):
    def test_no_inherit_future(self):
        # Two files: the first has "from __future__ import annotations", and it executes the second
        # which doesn't. The second file should not be affected by the first's __future__ imports.
        #
        # The annotations future became available in python 3.7 but has been replaced by PEP 649, so
        # it should remain supported but off-by-default for the foreseeable future.
        code1 = textwrap.dedent("""
            from __future__ import annotations
            from tornado.util import exec_in

            exec_in(code2, globals())
            """)

        code2 = textwrap.dedent("""
            def f(x: int) -> int:
                return x + 1
            output[0] = f.__annotations__
            """)

        # Make a mutable container to pass the result back to the caller
        output = [None]
        exec_in(code1, dict(code2=code2, output=output))
        # If the annotations future were in effect, these would be strings instead of the int type
        # object.
        self.assertEqual(output[0], {"x": int, "return": int})


class ArgReplacerTest(TestCase):
    def setUp(self):
        def function(x, y, callback=None, z=None):
            pass

        self.replacer = ArgReplacer(function, "callback")

    def test_omitted(self):
        args = (1, 2)
        kwargs: dict[str, Any] = dict()
        self.assertIsNone(self.replacer.get_old_value(args, kwargs))
        self.assertEqual(
            self.replacer.replace("new", args, kwargs),
            (None, (1, 2), dict(callback="new")),
        )

    def test_position(self):
        args = (1, 2, "old", 3)
        kwargs: dict[str, Any] = dict()
        self.assertEqual(self.replacer.get_old_value(args, kwargs), "old")
        self.assertEqual(
            self.replacer.replace("new", args, kwargs),
            ("old", [1, 2, "new", 3], dict()),
        )

    def test_keyword(self):
        args = (1,)
        kwargs = dict(y=2, callback="old", z=3)
        self.assertEqual(self.replacer.get_old_value(args, kwargs), "old")
        self.assertEqual(
            self.replacer.replace("new", args, kwargs),
            ("old", (1,), dict(y=2, callback="new", z=3)),
        )


class TimedeltaToSecondsTest(TestCase):
    def test_timedelta_to_seconds(self):
        time_delta = datetime.timedelta(hours=1)
        self.assertEqual(timedelta_to_seconds(time_delta), 3600.0)


class ImportObjectTest(TestCase):
    def test_import_member(self):
        self.assertIs(import_object("tornado.escape.utf8"), utf8)

    def test_import_member_unicode(self):
        self.assertIs(import_object("tornado.escape.utf8"), utf8)

    def test_import_module(self):
        self.assertIs(import_object("tornado.escape"), tornado.escape)

    def test_import_module_unicode(self):
        # The internal implementation of __import__ differs depending on
        # whether the thing being imported is a module or not.
        # This variant requires a byte string in python 2.
        self.assertIs(import_object("tornado.escape"), tornado.escape)


class ReUnescapeTest(TestCase):
    def test_re_unescape(self):
        test_strings = ("/favicon.ico", "index.html", "Hello, World!", "!$@#%;")
        for string in test_strings:
            self.assertEqual(string, re_unescape(re.escape(string)))

    def test_re_unescape_raises_error_on_invalid_input(self):
        with self.assertRaises(ValueError):
            re_unescape("\\d")
        with self.assertRaises(ValueError):
            re_unescape("\\b")
        with self.assertRaises(ValueError):
            re_unescape("\\Z")


class VersionInfoTest(TestCase):
    def assert_version_info_compatible(self, version, version_info):
        # We map our version identifier string (a subset of
        # https://packaging.python.org/en/latest/specifications/version-specifiers/#public-version-identifiers)
        # to a 4-tuple of integers for easy comparisons. The last component is
        # 0 for a final release, negative for a pre-release, and would be positive for a
        # post-release if we did any of those. This test is not a promise that these are the
        # only formats we will ever use, but it does catch accidents like
        # https://github.com/tornadoweb/tornado/issues/3406.
        major = minor = patch = "0"
        is_pre = False
        if m := re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version):
            # Regular 3-component version number
            major, minor, patch = m.groups()
        elif m := re.fullmatch(r"(\d+)\.(\d+)", version):
            # Two-component version number, equivalent to major.minor.0
            major, minor = m.groups()
        elif m := re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev|a|b|rc)\d+", version):
            # Pre-release 3-component version number.
            major, minor, patch = m.groups()
            is_pre = True
        elif m := re.fullmatch(r"(\d+)\.(\d+)(?:\.dev|a|b|rc)\d+", version):
            # Pre-release 2-component version number.
            major, minor = m.groups()
            is_pre = True
        else:
            self.fail(f"Unrecognized version format: {version}")

        self.assertEqual(version_info[:3], (int(major), int(minor), int(patch)))
        if is_pre:
            self.assertLess(int(version_info[3]), 0)
        else:
            self.assertEqual(int(version_info[3]), 0)

    def test_version_info_compatible(self):
        self.assert_version_info_compatible("6.5.0", (6, 5, 0, 0))
        self.assert_version_info_compatible("6.5", (6, 5, 0, 0))
        self.assert_version_info_compatible("6.5.1", (6, 5, 1, 0))
        self.assert_version_info_compatible("6.6.dev1", (6, 6, 0, -100))
        self.assert_version_info_compatible("6.6a1", (6, 6, 0, -100))
        self.assert_version_info_compatible("6.6b1", (6, 6, 0, -100))
        self.assert_version_info_compatible("6.6rc1", (6, 6, 0, -100))
        self.assertRaises(
            AssertionError, self.assert_version_info_compatible, "6.5.0", (6, 5, 0, 1)
        )
        self.assertRaises(
            AssertionError, self.assert_version_info_compatible, "6.5.0", (6, 4, 0, 0)
        )
        self.assertRaises(
            AssertionError, self.assert_version_info_compatible, "6.5.1", (6, 5, 0, 1)
        )

    def test_current_version(self):
        self.assert_version_info_compatible(tornado.version, tornado.version_info)


class GzipDecompressorTest(TestCase):
    # Sizes in which the compressed stream is handed to `decompress`,
    # standing in for the socket reads that drive it in `HTTP1Connection`.
    READ_SIZES = [None, 1, 3, 64, 1024]
    # Values of the ``max_length`` argument. ``_GzipMessageDelegate`` passes
    # the connection's ``chunk_size`` here, but the class also supports 0
    # (no limit).
    MAX_LENGTHS = [0, 1, 5, 100, 65536]

    def _compress(self, *members: bytes, padding: bytes = b"") -> bytes:
        return b"".join(gzip.compress(m) for m in members) + padding

    def _drive(self, data: bytes, read_size, max_length: int, size_limit: int) -> bytes:
        """Decompresses ``data`` the way `_GzipMessageDelegate` does.

        Follows the documented contract exactly: whatever ``decompress``
        leaves in ``unconsumed_tail`` is passed back in, and nothing else is.
        """
        decompressor = GzipDecompressor()
        result = bytearray()
        for i in range(0, len(data), read_size or max(len(data), 1)):
            chunk = data[i : i + (read_size or len(data))]
            while chunk:
                decompressed = decompressor.decompress(chunk, max_length)
                if max_length:
                    self.assertLessEqual(len(decompressed), max_length)
                result.extend(decompressed)
                # Input that is buffered internally must not also be
                # reported in unconsumed_tail: a caller that passes the tail
                # back in as documented would decompress it twice.
                self.assertLessEqual(len(result), size_limit, "duplicated output")
                chunk = decompressor.unconsumed_tail
                # `_GzipMessageDelegate.data_received` treats a non-empty
                # tail with no output as a failure to make progress.
                self.assertFalse(chunk and not decompressed, "no progress")
        result.extend(decompressor.flush())
        return bytes(result)

    def _assert_roundtrip(self, compressed: bytes, expected: bytes) -> None:
        for read_size in self.READ_SIZES:
            for max_length in self.MAX_LENGTHS:
                with self.subTest(read_size=read_size, max_length=max_length):
                    self.assertEqual(
                        self._drive(compressed, read_size, max_length, len(expected)),
                        expected,
                    )

    def test_single_member(self):
        # A single member must keep working at every chunk size; splitting a
        # member across `decompress` calls is the common case for any
        # response larger than ``chunk_size``.
        data = b"".join(b"Hello World %d\n" % i for i in range(1000))
        self._assert_roundtrip(self._compress(data), data)

    def test_empty_member(self):
        self._assert_roundtrip(self._compress(b""), b"")

    def test_concatenated_members(self):
        # Concatenated members are a single valid gzip stream (RFC 1952).
        members = [b"first member\n", b"second member\n" * 1000, b"third member\n"]
        self._assert_roundtrip(self._compress(*members), b"".join(members))

    def test_many_concatenated_members(self):
        members = [b"member %d\n" % i for i in range(100)]
        self._assert_roundtrip(self._compress(*members), b"".join(members))

    def test_trailing_padding(self):
        # Trailing zero bytes are ignored rather than treated as a truncated
        # member (https://www.gzip.org/ancient/#faq8).
        data = b"padded\n" * 100
        self._assert_roundtrip(self._compress(data, padding=b"\0" * 16), data)

    def test_padding_between_members(self):
        compressed = gzip.compress(b"one") + b"\0" * 8 + gzip.compress(b"two")
        self._assert_roundtrip(compressed, b"onetwo")

    def test_matches_stdlib(self):
        # Whatever we do with member boundaries and padding, the result must
        # be what `gzip.decompress` produces for the same bytes.
        for compressed in [
            self._compress(b"only one"),
            self._compress(b"one", b"two", b"three"),
            self._compress(b"padded", padding=b"\0" * 4),
        ]:
            with self.subTest(compressed=compressed):
                self._assert_roundtrip(compressed, gzip.decompress(compressed))

    def test_unconsumed_tail_is_not_buffered_twice(self):
        # The tail reported to the caller must not also be retained
        # internally: a caller that follows the contract would otherwise
        # decompress the same bytes twice.
        data = b"0123456789" * 10
        decompressor = GzipDecompressor()
        first = decompressor.decompress(self._compress(data), 10)
        self.assertEqual(first, data[:10])
        tail = decompressor.unconsumed_tail
        self.assertTrue(tail)
        rest = decompressor.decompress(tail)
        self.assertEqual(first + rest + decompressor.flush(), data)

    def test_decompress_with_no_input(self):
        decompressor = GzipDecompressor()
        self.assertEqual(decompressor.decompress(b""), b"")
        self.assertEqual(decompressor.unconsumed_tail, b"")
        self.assertEqual(decompressor.decompress(self._compress(b"data"), 2), b"da")
        # An empty read does not discard the tail.
        self.assertEqual(decompressor.decompress(b""), b"")
        self.assertEqual(decompressor.decompress(decompressor.unconsumed_tail), b"ta")

    def test_corrupted_checksum(self):
        compressed = bytearray(self._compress(b"hello"))
        compressed[-5] ^= 0xFF
        with self.assertRaises(zlib.error):
            self._drive(bytes(compressed), None, 0, 1000)

    def test_corrupted_second_member(self):
        compressed = bytearray(self._compress(b"hello", b"world"))
        # Break the magic number of the second member.
        compressed[len(gzip.compress(b"hello"))] ^= 0xFF
        with self.assertRaises(zlib.error):
            self._drive(bytes(compressed), None, 0, 1000)

    def test_trailing_garbage(self):
        with self.assertRaises(zlib.error):
            self._drive(self._compress(b"hello", padding=b"garbage"), None, 0, 1000)

    def test_decompress_after_flush(self):
        decompressor = GzipDecompressor()
        decompressor.decompress(self._compress(b"hello"))
        decompressor.flush()
        with self.assertRaises(RuntimeError):
            decompressor.decompress(self._compress(b"more"))
        with self.assertRaises(RuntimeError):
            decompressor.flush()

    def test_flush_returns_remaining_members(self):
        # A caller that stops calling decompress early still gets everything
        # from flush(), as it would for a partially-consumed single member.
        decompressor = GzipDecompressor()
        compressed = self._compress(b"one", b"two")
        self.assertEqual(decompressor.decompress(compressed, 2), b"on")
        self.assertEqual(decompressor.flush(), b"etwo")


class LogCheckCoverageTest(TestCase):
    def test_all_test_classes_check_logs(self):
        """Every test in the suite must fail if it logs something unexpected.

        New test classes must derive from the base classes in
        `tornado.test.util` rather than from `unittest` or `tornado.testing`
        directly, so that the check in `.TestCase.setUp` applies to them.
        """
        import doctest

        from tornado.test.runtests import all as all_tests
        from tornado.test.util import AsyncTestCase as UtilAsyncTestCase

        def flatten(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    yield from flatten(test)
                else:
                    yield test

        uncovered = set()
        for test in flatten(all_tests()):
            cls = type(test)
            if cls.__module__ == "unittest.loader":
                # Placeholders for modules that failed to import; these are
                # reported as errors by the tests that use them.
                continue
            if issubclass(cls, doctest.DocTestCase):
                # Doctests are built by doctest.DocTestSuite and cannot be
                # given a base class of our own.
                continue
            if not issubclass(cls, (TestCase, UtilAsyncTestCase)):
                uncovered.add(f"{cls.__module__}.{cls.__qualname__}")
        self.assertEqual(uncovered, set())
