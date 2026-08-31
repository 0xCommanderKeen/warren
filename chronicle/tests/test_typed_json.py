"""The typed-JSON codec's exact byte obligations, stated as golden values.

These were once cross-checked against a JavaScript twin (``viewer/typed-json.js``).
The viewer is gone, but the byte format outlived it: rotated logs on disk carry
mood-authority records encoded with it, so the escaping and the binary64 tokens
are still a wire format, not an implementation detail.  Golden strings below are
the contract; changing one is a log-format migration, not a refactor.
"""

import json
import math
import pathlib
import unittest
from types import MappingProxyType

from typed_json import (
    canonical_string,
    decode_graph,
    freeze_json,
    semantic_key,
    thaw_json,
    typed_graph,
)


class CanonicalStringEscaping(unittest.TestCase):
    """``canonical_string`` is the one escaper; it had two other copies."""

    def test_short_escapes_use_the_two_character_forms(self):
        self.assertEqual(
            canonical_string("\b\t\n\f\r"), '"' + "\\b\\t\\n\\f\\r" + '"'
        )

    def test_quote_and_backslash_are_escaped(self):
        self.assertEqual(canonical_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_remaining_control_characters_use_lowercase_four_digit_hex(self):
        self.assertEqual(canonical_string("\x00"), '"\\u0000"')
        self.assertEqual(canonical_string("\x01\x1f"), '"\\u0001\\u001f"')
        self.assertEqual(canonical_string("\x0b"), '"\\u000b"')

    def test_delete_is_escaped_but_the_rest_of_ascii_is_literal(self):
        self.assertEqual(canonical_string("\x7f"), '"\\u007f"')
        self.assertEqual(canonical_string(" ~!/"), '" ~!/"')

    def test_non_ascii_bmp_characters_escape_to_their_code_unit(self):
        self.assertEqual(canonical_string("é"), '"\\u00e9"')
        self.assertEqual(canonical_string(" ￿"), '"\\u2028\\uffff"')

    def test_non_bmp_characters_escape_as_utf16_surrogate_pairs(self):
        self.assertEqual(canonical_string("\U0001f600"), '"\\ud83d\\ude00"')
        self.assertEqual(canonical_string("\U0010ffff"), '"\\udbff\\udfff"')

    def test_lone_surrogates_survive_as_their_own_code_unit(self):
        # Python strings may carry unpaired surrogates (surrogatepass decoding);
        # the escaper must not raise or silently substitute.
        self.assertEqual(canonical_string("\ud800"), '"\\ud800"')

    def test_empty_string_is_a_pair_of_quotes(self):
        self.assertEqual(canonical_string(""), '""')


class TypedGraphEncoding(unittest.TestCase):
    """Node tags and binary64 tokens are the durable half of the format."""

    def test_scalars_carry_their_tag(self):
        self.assertEqual(typed_graph(None), [[["n"]], 0])
        self.assertEqual(typed_graph(True), [[["b", 1]], 0])
        self.assertEqual(typed_graph(False), [[["b", 0]], 0])
        self.assertEqual(typed_graph("hi"), [[["s", "hi"]], 0])

    def test_numbers_encode_as_exact_big_endian_binary64_hex(self):
        self.assertEqual(typed_graph(1)[0][0], ["f", "3ff0000000000000"])
        self.assertEqual(typed_graph(1.0)[0][0], ["f", "3ff0000000000000"])
        self.assertEqual(typed_graph(0.1)[0][0], ["f", "3fb999999999999a"])
        self.assertEqual(typed_graph(-1.5)[0][0], ["f", "bff8000000000000"])

    def test_both_zeroes_normalize_to_the_positive_zero_token(self):
        self.assertEqual(typed_graph(0)[0][0], ["f", "0000000000000000"])
        self.assertEqual(typed_graph(-0.0)[0][0], ["f", "0000000000000000"])

    def test_nonfinite_numbers_share_one_token(self):
        self.assertEqual(typed_graph(math.inf)[0][0], ["f", "nonfinite"])
        self.assertEqual(typed_graph(math.nan)[0][0], ["f", "nonfinite"])

    def test_unsupported_values_become_the_opaque_tag(self):
        self.assertEqual(typed_graph(object())[0][0], ["x"])

    def test_children_always_precede_their_parent(self):
        nodes, root = typed_graph(["a", ["b"]])
        self.assertEqual(root, len(nodes) - 1)
        self.assertEqual(nodes[-1][0], "a")

    def test_object_keys_are_sorted_by_their_escaped_form(self):
        nodes, _ = typed_graph({"b": 1, "a": 2, "A": 3})
        self.assertEqual([key for key, _ in nodes[-1][1]], ["A", "a", "b"])

    def test_cycles_and_shared_references_are_refused(self):
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(ValueError):
            typed_graph(cycle)
        shared = ["x"]
        with self.assertRaises(ValueError):
            typed_graph([shared, shared])

    def test_deep_values_do_not_exhaust_the_recursion_stack(self):
        value = "leaf"
        for _ in range(5000):
            value = {"next": value}
        self.assertEqual(decode_graph(typed_graph(value)), value)


class SemanticKeyIdentity(unittest.TestCase):
    """The key is a string, so it is hashable, comparable and loggable."""

    def test_key_is_the_canonical_shallow_json_of_the_graph(self):
        self.assertEqual(semantic_key(None), '[[["n"]],0]')
        self.assertEqual(semantic_key("hi"), '[[["s","hi"]],0]')
        self.assertEqual(
            semantic_key({"a": [True]}),
            '[[["b",1],["a",[0]],["o",[["a",1]]]],2]',
        )

    def test_key_ignores_insertion_order_of_object_keys(self):
        self.assertEqual(semantic_key({"a": 1, "b": 2}), semantic_key({"b": 2, "a": 1}))

    def test_key_ignores_the_int_float_distinction_of_the_ieee754_domain(self):
        self.assertEqual(semantic_key(1), semantic_key(1.0))

    def test_key_separates_values_that_json_dumps_would_confuse(self):
        self.assertNotEqual(semantic_key(1), semantic_key("1"))
        self.assertNotEqual(semantic_key(None), semantic_key("null"))
        self.assertNotEqual(semantic_key([]), semantic_key({}))

    def test_key_escapes_string_content_rather_than_embedding_it_raw(self):
        self.assertEqual(semantic_key('"'), '[[["s","\\""]],0]')
        self.assertEqual(semantic_key("\U0001f600"), '[[["s","\\ud83d\\ude00"]],0]')

    def test_keys_built_from_confusable_strings_stay_distinct(self):
        self.assertNotEqual(semantic_key(['a"],["b']), semantic_key(["a", "b"]))


class GraphDecoding(unittest.TestCase):
    """Decoding is hostile-input hardened: it validates before it restores."""

    def test_round_trip_restores_json_values(self):
        for value in (
            None,
            True,
            "",
            "text",
            0.1,
            [],
            {},
            [1, "two", None, {"k": [False]}],
            {"nested": {"deep": [1.5, -2.5]}},
        ):
            with self.subTest(value=value):
                self.assertEqual(decode_graph(typed_graph(value)), value)

    def test_round_trip_normalizes_containers_and_integral_floats(self):
        self.assertEqual(decode_graph(typed_graph((1, 2))), [1, 2])
        self.assertEqual(decode_graph(typed_graph(MappingProxyType({"a": 1}))), {"a": 1})
        restored = decode_graph(typed_graph(2.0))
        self.assertEqual(restored, 2)
        self.assertIsInstance(restored, int)

    def test_round_trip_keeps_fractional_and_huge_numbers_as_floats(self):
        self.assertIsInstance(decode_graph(typed_graph(0.5)), float)
        self.assertIsInstance(decode_graph(typed_graph(1e300)), float)

    def test_malformed_envelopes_are_refused(self):
        for graph in (None, [], [[]], [[["n"]]], [[["n"]], "0"], [[["n"]], True]):
            with self.subTest(graph=graph):
                with self.assertRaises(ValueError):
                    decode_graph(graph)

    def test_unknown_and_malformed_nodes_are_refused(self):
        for nodes in (
            [["z"]],
            [[]],
            ["n"],
            [["b", 2]],
            [["b", True]],
            [["s", 1]],
            [["n", 1]],
        ):
            with self.subTest(nodes=nodes):
                with self.assertRaises(ValueError):
                    decode_graph([nodes, len(nodes) - 1])

    def test_noncanonical_binary64_tokens_are_refused(self):
        for token in ("", "3FF0000000000000", "zzzzzzzzzzzzzzzz", "3ff000000000000"):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    decode_graph([[["f", token]], 0])
        # Negative zero and the nonfinite bit patterns have one spelling each.
        with self.assertRaises(ValueError):
            decode_graph([[["f", "8000000000000000"]], 0])
        with self.assertRaises(ValueError):
            decode_graph([[["f", "7ff0000000000000"]], 0])

    def test_forward_and_out_of_range_references_are_refused(self):
        with self.assertRaises(ValueError):
            decode_graph([[["a", [0]]], 0])
        with self.assertRaises(ValueError):
            decode_graph([[["s", "x"], ["a", [5]]], 1])

    def test_unsorted_or_repeated_object_keys_are_refused(self):
        with self.assertRaises(ValueError):
            decode_graph([[["n"], ["n"], ["o", [["b", 0], ["a", 1]]]], 2])
        with self.assertRaises(ValueError):
            decode_graph([[["n"], ["n"], ["o", [["a", 0], ["a", 1]]]], 2])

    def test_shared_and_unreferenced_nodes_are_refused(self):
        with self.assertRaises(ValueError):
            decode_graph([[["n"], ["a", [0, 0]]], 1])
        with self.assertRaises(ValueError):
            decode_graph([[["n"], ["s", "orphan"], ["a", [0]]], 2])

    def test_root_must_be_the_last_node(self):
        with self.assertRaises(ValueError):
            decode_graph([[["n"], ["a", [0]]], 0])


class FreezeAndThaw(unittest.TestCase):
    """Structural sharing is the bug these two exist to prevent."""

    def test_freeze_detaches_and_makes_containers_immutable(self):
        source = {"list": [1, {"deep": 2}]}
        frozen = freeze_json(source)
        source["list"][0] = "mutated"
        self.assertEqual(frozen["list"][0], 1)
        self.assertIsInstance(frozen, MappingProxyType)
        self.assertIsInstance(frozen["list"], tuple)
        with self.assertRaises(TypeError):
            frozen["list"] = []

    def test_thaw_returns_ordinary_containers_for_serialization(self):
        thawed = thaw_json(freeze_json({"list": [1], "map": {"a": 2}}))
        self.assertEqual(thawed, {"list": [1], "map": {"a": 2}})
        self.assertIsInstance(thawed, dict)
        self.assertIsInstance(thawed["list"], list)

    def test_scalars_pass_through_both_directions(self):
        for value in (None, True, 1, 1.5, "text"):
            with self.subTest(value=value):
                self.assertEqual(thaw_json(freeze_json(value)), value)

    def test_cycles_and_shared_references_are_refused(self):
        cycle = {}
        cycle["self"] = cycle
        with self.assertRaises(ValueError):
            freeze_json(cycle)
        shared = {"a": 1}
        with self.assertRaises(ValueError):
            freeze_json([shared, shared])

    def test_deep_values_do_not_exhaust_the_recursion_stack(self):
        value = {}
        for _ in range(5000):
            value = {"next": value}
        self.assertEqual(thaw_json(freeze_json(value)), value)


class RetiredParityVectors(unittest.TestCase):
    """``mood-capsule-parity.json`` outlived the JavaScript half it was built for.

    The 48 tokens were the shared vector proving both languages agreed on
    binary64 spelling.  With one language left they still pin something real —
    that these exact bit patterns, negatives and extreme exponents included,
    survive a decode/re-encode unchanged — so the file goes back to work instead
    of being deleted with the viewer.
    """

    VECTORS = json.loads(
        (
            pathlib.Path(__file__).parent / "fixtures" / "mood-capsule-parity.json"
        ).read_text(encoding="utf-8")
    )["finite_binary64_bits"]

    def test_the_vector_file_is_still_populated(self):
        self.assertEqual(len(self.VECTORS), 48)

    def test_every_vector_token_survives_a_decode_and_re_encode(self):
        for token in self.VECTORS:
            with self.subTest(token=token):
                graph = [[["f", token]], 0]
                value = decode_graph(graph)
                self.assertIsInstance(value, float)
                self.assertEqual(typed_graph(value)[0][0][1], token)
                self.assertEqual(semantic_key(value), '[[["f","' + token + '"]],0]')


class DurableByteObligations(unittest.TestCase):
    """What the retired cross-language parity fixtures now guard: the disk.

    ``retention._encode_mood_authority`` writes these bytes into rotated logs
    and reads them back on the next boot, so the escaper and the graph encoding
    are pinned by data that already exists, not by a second implementation.
    """

    def test_mood_authority_record_is_byte_stable(self):
        import retention

        encoded = retention._encode_mood_authority(
            {"_burrow_internal": "mood-authority-v1", "mood": "calm", "score": 0.5}
        )
        self.assertEqual(
            encoded,
            '{"_burrow_internal":"mood-authority-v1","encoding":"typed-binary64-v1"'
            ',"graph":[[["s","calm"],["f","3fe0000000000000"],'
            '["o",[["mood",0],["score",1]]]],2]}',
        )

    def test_mood_authority_round_trips_through_the_decoder(self):
        import json

        import retention

        logical = {"mood": "calm", "score": 0.5, "note": "hi\nthere \U0001f600"}
        record = json.loads(retention._encode_mood_authority(logical))
        self.assertEqual(decode_graph(record["graph"]), logical)


if __name__ == "__main__":
    unittest.main()
