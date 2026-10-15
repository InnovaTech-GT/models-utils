"""
Playbook template filters (doc 34).

The rendered output of these templates is executed on production network
hardware, so the security cases are not decoration — every adversarial input
below was proposed by the design panel's security reviewer and must fail
closed. A filter chain is a closed dispatch table, NOT an expression language.

Moved from backend-erp tests/provisioning/test_template_filters.py (provisioning worker split, R1).
(the renderer half). The routers.playbooks save-time cases and the
executor-source check stay in backend-erp.
"""

import pytest

from database_utils.utils import playbook_template as renderer
from database_utils.utils.playbook_render import _leftover_tokens, _render_step
from database_utils.utils.playbook_template import (
    TOKEN_SHAPE,
    TemplateError,
    find_template_errors,
    find_variables,
    parse_body,
    render_string,
    render_value,
)


def r(template, variables=None):
    return render_string(template, variables or {})


class TestBackCompat:
    """Every stored playbook must keep rendering byte-identically."""

    @pytest.mark.parametrize("template,variables,expected", [
        ("{{client.name}}", {"client.name": "Ada"}, "Ada"),
        ("vlan {{service_plan.vlan}}", {"service_plan.vlan": 110}, "vlan 110"),
        ("{{ edge_devices[0].serial }}", {"edge_devices[0].serial": "S1"}, "S1"),
        ("{{chain[1].item_id}}", {"chain[1].item_id": "i1"}, "i1"),
    ])
    def test_plain_tokens_unchanged(self, template, variables, expected):
        out, problems = r(template, variables)
        assert (out, problems) == (expected, [])

    def test_missing_variable_still_reports_the_bare_name(self):
        out, problems = r("{{client.name}}", {})
        assert out == "{{client.name}}"
        assert problems == ["client.name"]

    def test_non_token_braces_are_still_left_alone_silently(self):
        """`{{ not a token }}` has always been literal text and must stay so —
        there is no pipe, so the author never reached for a filter."""
        out, problems = r("{{ not a token }}", {})
        assert out == "{{ not a token }}"
        assert problems == []

    def test_find_variables_contract_unchanged(self):
        assert find_variables("{{a.b}} {{c.d}} {{a.b}}") == ["a.b", "c.d"]


class TestFilters:
    @pytest.mark.parametrize("template,value,expected", [
        ("{{x | upper}}", "abc", "ABC"),
        ("{{x | lower}}", "ABC", "abc"),
        ("{{x | trim}}", "  ab  ", "ab"),
        ("{{x | alnum}}", "a b-c_d!'\"\n", "ab-c_d"),
        ('{{x | replace:"a","b"}}', "banana", "bbnbnb"),
        ("{{x | truncate:3}}", "abcdef", "abc"),
        ('{{x | pad_left:5,"0"}}', "42", "00042"),
        ("{{x | pad_left:3}}", "4", "  4"),
    ])
    def test_basic(self, template, value, expected):
        assert r(template, {"x": value}) == (expected, [])

    @pytest.mark.parametrize("style,expected", [
        ("colon", "AA:BB:CC:DD:EE:FF"),
        ("dash", "AA-BB-CC-DD-EE-FF"),
        ("dot", "aabb.ccdd.eeff"),
        ("bare", "AABBCCDDEEFF"),
    ])
    def test_mac_styles(self, style, expected):
        out, problems = r("{{x | mac:%s}}" % style, {"x": "aa:bb:cc:dd:ee:ff"})
        assert (out, problems) == (expected, [])

    def test_mac_normalises_any_input_separator(self):
        for raw in ("AABBCCDDEEFF", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff"):
            assert r("{{x | mac:colon}}", {"x": raw})[0] == "AA:BB:CC:DD:EE:FF"

    def test_pad_left_never_truncates(self):
        """Silently cutting an identifier is worse than an over-long one."""
        assert r("{{x | pad_left:2}}", {"x": "abcdef"})[0] == "abcdef"

    def test_chaining_is_left_to_right(self):
        out, problems = r('{{x | trim | replace:" ","_" | upper}}', {"x": "  a b  "})
        assert (out, problems) == ("A_B", [])

    def test_whitespace_around_pipes_is_free(self):
        for template in ("{{x|upper}}", "{{ x | upper }}", "{{x |upper}}", "{{x| upper }}"):
            assert r(template, {"x": "a"})[0] == "A"


class TestCoercion:
    @pytest.mark.parametrize("value,plain,uppered", [
        ("abc", "abc", "ABC"),
        (110, "110", "110"),
        (True, "True", "TRUE"),
        (0, "0", "0"),
        (False, "False", "FALSE"),
        ("", "", ""),
    ])
    def test_non_strings(self, value, plain, uppered):
        assert r("{{x}}", {"x": value})[0] == plain
        assert r("{{x | upper}}", {"x": value})[0] == uppered

    def test_zero_and_false_are_present_not_missing(self):
        """No falsy trap: 0 is a real VLAN-adjacent value."""
        for value in (0, False, ""):
            assert r("{{x}}", {"x": value})[1] == []

    def test_none_is_missing(self):
        assert r("{{x}}", {"x": None})[1] == ["x"]


class TestDefault:
    def test_rescues_a_missing_variable(self):
        assert r('{{x | default:"guest"}}', {}) == ("guest", [])

    def test_rescues_none_and_blank(self):
        assert r('{{x | default:"g"}}', {"x": None}) == ("g", [])
        assert r('{{x | default:"g"}}', {"x": "   "}) == ("g", [])

    def test_filters_after_default_apply_to_it(self):
        assert r('{{x | default:"guest" | upper}}', {}) == ("GUEST", [])

    def test_filters_before_a_default_are_skipped_when_missing(self):
        assert r('{{x | upper | default:"X"}}', {}) == ("X", [])

    def test_present_value_ignores_the_default(self):
        assert r('{{x | default:"guest"}}', {"x": "ada"}) == ("ada", [])


class TestMissingWithFilters:
    def test_missing_reports_the_bare_name_and_leaves_the_construct(self):
        out, problems = r("{{client.name | upper}}", {})
        assert out == "{{client.name | upper}}"
        assert problems == ["client.name"]


class TestStaticErrors:
    @pytest.mark.parametrize("template,fragment", [
        ("{{x | uppr}}", "unknown filter 'uppr'"),
        ("{{x | truncate}}", "argument(s), got 0"),
        ('{{x | replace:"a"}}', "argument(s), got 1"),
        ("{{x | truncate:0}}", "between 1 and 512"),
        ("{{x | truncate:abc}}", "whole number"),
        ("{{x | mac:hex}}", "style must be one of"),
        ('{{x | replace:"a}}', "unterminated quoted argument"),
        (r'{{x | replace:"a","\x41"}}', "invalid escape"),
        ('{{x | replace:"a"x}}', "unexpected"),
        ("{{x | upper | lower | trim | alnum | upper}}", "too many filters"),
        ('{{x | pad_left:3,"ab"}}', "exactly one character"),
        ('{{x | replace:"","b"}}', "non-empty first argument"),
    ])
    def test_reported_and_construct_left_literal(self, template, fragment):
        out, problems = r(template, {"x": "value"})
        assert out == template, "a broken construct must not be substituted"
        assert any(fragment in p for p in problems), problems

    def test_typo_gets_a_suggestion(self):
        _out, problems = r("{{x | uppr}}", {"x": "a"})
        assert "Did you mean 'upper'" in problems[0]

    def test_static_errors_are_findable_without_variables(self):
        """This is what makes save-time rejection possible."""
        assert find_template_errors("{{x | uppr}}")
        assert find_template_errors("{{x | upper}}") == []
        # No pipe -> ordinary literal text, not an error.
        assert find_template_errors("{{ not a token }}") == []


class TestRuntimeErrors:
    def test_mac_on_non_mac_errors_rather_than_passing_garbage(self):
        out, problems = r("{{x | mac:dash}}", {"x": "ABC123"})
        assert out == "{{x | mac:dash}}"
        assert any("is not a MAC address" in p for p in problems)

    def test_output_size_is_capped(self):
        out, problems = r('{{x | pad_left:64,"0"}}', {"x": "a" * 10})
        assert problems == []  # well under the cap
        big = "a" * (renderer.MAX_OUTPUT + 10)
        _out, problems = r("{{x | upper}}", {"x": big})
        assert any("more than" in p for p in problems)


class TestSecurity:
    """Every case here was proposed by the design panel's security reviewer."""

    @pytest.mark.parametrize("template", [
        '{{ x | attr:"__globals__" }}',
        '{{ x | getattr:"__class__" }}',
        '{{ x | regex_replace:".*","o" }}',
        '{{ x | eval:"1" }}',
    ])
    def test_dangerous_filters_are_rejected(self, template):
        out, problems = r(template, {"x": "a"})
        assert out == template
        assert any("unknown filter" in p for p in problems)

    def test_a_brace_bearing_body_is_not_a_token_at_all(self):
        """The str.format gadget `{0.__class__}` cannot even be expressed: a
        body containing a brace is not token-shaped, so it is never parsed and
        never substituted. It survives as literal text, which the executor's
        guard then refuses to ship."""
        template = '{{ x | format:"{0.__class__}" }}'
        out, problems = r(template, {"x": "a"})
        assert out == template
        _rendered, unresolved = _render_step({"template": template}, {"x": "a"}, "simulator")
        assert unresolved, "must not be shipped to a device"

    @pytest.mark.parametrize("name", ["format", "attr", "getattr", "eval", "exec", "regex_replace"])
    def test_not_in_the_table(self, name):
        assert name not in renderer.FILTERS

    def test_no_dynamic_dispatch_in_the_source(self):
        """A getattr-based dispatch would turn a tenant-writable playbook row
        into code execution inside the worker that holds SSH credentials for
        every customer's core network. Assert the gadgets are absent."""
        import inspect
        source = inspect.getsource(renderer)
        # `re.compile(` is legitimate and expected; the gadgets below are the
        # ones that would reintroduce dynamic dispatch or attribute reach.
        for gadget in ("getattr(", "eval(", "exec(", "__import__", ".format(",
                       "__class__", "__globals__", "__builtins__"):
            assert gadget not in source, f"{gadget} must never appear in the renderer"

    def test_attribute_access_is_not_expressible(self):
        out, problems = r("{{ ''.__class__ }}", {})
        assert out == "{{ ''.__class__ }}"
        assert problems == []  # no pipe -> literal, and the executor guard catches it

    def test_a_value_containing_a_token_is_inert(self):
        """A tenant CRM field holding template text must not be re-rendered."""
        out, problems = r("{{x}}", {"x": "{{ client.name | upper }}"})
        assert out == "{{ client.name | upper }}"
        assert problems == []
        # ...but the executor's guard refuses to ship it.
        assert _leftover_tokens(out)

    def test_arguments_cannot_name_a_variable(self):
        """`replace:":",{{y}}` is not expressible. The outer construct contains
        a brace so it is not token-shaped and is never parsed as a filter
        chain; whatever survives is refused by the executor's guard rather than
        quietly becoming a working variable-valued argument."""
        template = '{{ x | replace:":",{{y}} }}'
        out, _problems = r(template, {"x": "a", "y": "b"})
        assert "replace" in out, "the filter chain must not have been applied"
        _rendered, unresolved = _render_step(
            {"template": template}, {"x": "a", "y": "b"}, "simulator")
        assert unresolved, "must not be shipped to a device"

    def test_braces_cannot_appear_in_an_argument(self):
        """No filter output can manufacture token-shaped text, because a body
        containing a brace is not a token in the first place."""
        template = '{{ x | replace:"a","{" }}'
        out, problems = r(template, {"x": "a"})
        assert out == template and problems == []
        assert TOKEN_SHAPE.search("{{ a{b }}") is None

    def test_backreference_text_is_literal(self):
        r"""`\1` in a replacement must stay literal — the substitution callback
        is a function, so re never expands it."""
        out, problems = r(r'{{ x | replace:"a","\\1" }}', {"x": "a"})
        assert problems == []
        assert out == "\\1"

    def test_filters_are_refused_on_secret_variables(self):
        """A transformed secret cannot be scrubbed from the job log, because
        redaction matches the raw plaintext."""
        for name in ("input.wifi_password", "input.api_key", "input.auth_token",
                     "input.client_secret", "input.credential_x"):
            out, problems = r("{{%s | upper}}" % name, {name: "hunter2"})
            assert out == "{{%s | upper}}" % name
            assert any("not allowed on secret" in p for p in problems), name

    def test_a_secret_still_renders_untransformed(self):
        """The ban is on transforming, not on using."""
        assert r("{{input.wifi_password}}", {"input.wifi_password": "hunter2"}) == ("hunter2", [])

    def test_mask_secrets_still_masks(self):
        masked = renderer.mask_secrets({"input.wifi_password": "hunter2", "client.name": "Ada"})
        assert masked == {"input.wifi_password": "***", "client.name": "Ada"}


class TestGuardIntegration:
    """The renderer and the executor's guard must share ONE recognizer."""

    @pytest.mark.parametrize("template", [
        "{{client.name.upper(}}",   # the method-call design's confirmed hole
        "{{a:1}}",                  # the suffix design's confirmed hole
        "{{Edge_Devices[0].serial}}",
        "{{device1_serial}}",       # a retired flat name
    ])
    def test_everything_token_shaped_is_caught(self, template):
        """Anything the strict parser refuses is still token-SHAPED, so the
        guard fails the step rather than shipping it to a device."""
        rendered, unresolved = _render_step({"template": template}, {}, "simulator")
        assert unresolved, f"{template} would have been shipped literally"

    def test_a_problem_is_reported_once_not_twice(self):
        _rendered, unresolved = _render_step(
            {"template": "{{client.name}}"}, {}, "simulator")
        assert unresolved == ["client.name"]

    def test_filter_error_reaches_the_step_as_a_problem(self):
        _rendered, unresolved = _render_step(
            {"template": "{{x | uppr}}"}, {"x": "a"}, "simulator")
        assert len(unresolved) == 1
        assert "unknown filter" in unresolved[0]

    def test_request_payloads_are_filtered_too(self):
        out, problems = render_value(
            {"body": {"mac": "{{x | mac:dash}}"}}, {"x": "aabbccddeeff"})
        assert out == {"body": {"mac": "AA-BB-CC-DD-EE-FF"}}
        assert problems == []

    def test_json_braces_are_not_mistaken_for_tokens(self):
        assert _leftover_tokens('{"a": {"b": 1}}') == []


class TestParseBodyDirectly:
    def test_returns_name_and_chain(self):
        expr = parse_body(' client.name | replace:"a","b" | upper ')
        assert expr.name == "client.name"
        assert expr.filters == (("replace", ("a", "b")), ("upper", ()))

    def test_empty_quoted_argument_is_legal(self):
        assert parse_body('x | replace:",",""').filters == (("replace", (",", "")),)

    @pytest.mark.parametrize("body", ["x |", "x | | upper", "| upper", "x y"])
    def test_malformed(self, body):
        with pytest.raises(TemplateError):
            parse_body(body)


class TestSharedFixture:
    """The frontend mirrors this grammar. A divergence between the two has
    already caused a real bug here (the editor preview rendered a token the
    live run left literal), so the fixture is shared byte-for-byte and locked
    by hash: editing one copy without the other fails CI in that repo."""

    FIXTURE = "tests/fixtures/playbook_filters.json"
    SHA256 = "4438578fbaf534ce9f3ae41fb743f56c4dc2f3fa20d90330dda76aa50a05e4b8"

    def test_fixture_hash_is_locked(self):
        import hashlib, pathlib
        data = pathlib.Path(self.FIXTURE).read_bytes()
        assert hashlib.sha256(data).hexdigest() == self.SHA256, (
            "the shared filter fixture changed — update the copy in "
            "frontend-erp/lib/__fixtures__/playbookFilters.json and both SHA constants"
        )

    def test_every_case_matches(self):
        import json, pathlib
        for case in json.loads(pathlib.Path(self.FIXTURE).read_text()):
            out, problems = render_string(case["template"], case["variables"])
            assert out == case["expected"], case["template"]
            if case.get("error"):
                assert problems, case["template"]
            if case.get("problems"):
                assert problems == case["problems"], case["template"]


class TestClientNameForADeviceCli:
    """The founder's stated case: a device CLI rejects spaces in a name.

    Pinned explicitly because it is the motivating example for the whole
    feature, and because the quoted-space argument is the fiddly bit an
    author is most likely to get wrong.
    """

    NAME = "E2E TEST CLIENT"

    @pytest.mark.parametrize("template,expected", [
        ('{{client.name | replace:" ","_"}}', "E2E_TEST_CLIENT"),
        ('{{client.name | replace:" ","-"}}', "E2E-TEST-CLIENT"),
        ('{{client.name | replace:" ",""}}', "E2ETESTCLIENT"),
        ('{{client.name|replace:" ","_"}}', "E2E_TEST_CLIENT"),
        ('{{ client.name | replace:" ","_" }}', "E2E_TEST_CLIENT"),
        # realistic full chain for a description field
        ('{{client.name | trim | replace:" ","_" | truncate:12}}', "E2E_TEST_CLI"),
        # alnum drops spaces too, but loses the word boundaries
        ("{{client.name | alnum}}", "E2ETESTCLIENT"),
    ])
    def test_spaces_can_be_replaced(self, template, expected):
        assert render_string(template, {"client.name": self.NAME}) == (expected, [])

    def test_a_quoted_space_is_a_real_argument_not_an_empty_one(self):
        expr = parse_body('client.name | replace:" ","_"')
        assert expr.filters == (("replace", (" ", "_")),)

    def test_leading_and_trailing_spaces_survive_replace(self):
        """`replace` is literal and total — it does not strip first. Chain
        `trim` before it if that is what you meant."""
        out, _ = render_string('{{x | replace:" ","_"}}', {"x": " a b "})
        assert out == "_a_b_"
