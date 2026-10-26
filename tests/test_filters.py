from applypilot import config
from applypilot.apply import launcher

ACCEPT = ["India", "Bengaluru", "APAC"]
REJECT = ["Remote - US", "US only"]


def test_location_india_and_global_remote_pass():
    for loc in ["Bengaluru", "Remote - India", "Remote", "Remote, Global", "APAC (Remote)", None]:
        assert config.location_ok(loc, ACCEPT, REJECT), loc


def test_location_country_restricted_remote_rejected():
    for loc in ["San Francisco (Remote)", "Remote US", "Remote - US", "London, UK (Remote)", "Chicago"]:
        assert not config.location_ok(loc, ACCEPT, REJECT), loc


def test_short_pattern_needs_whole_word():
    assert not config.location_ok("Chicago, IL", ["CA"], [])
    assert config.location_ok("Los Angeles, CA", ["CA"], [])


def test_location_filters_reads_documented_and_legacy_keys():
    cfg = {"location": {"accept_patterns": ["India"], "reject_patterns": ["US only"]},
           "location_accept": ["Pune"], "location_reject_non_remote": ["Dubai"]}
    assert config.location_filters(cfg) == (["India", "Pune"], ["US only", "Dubai"])


def test_mcp_config_pins_versions_and_gmail_is_opt_in(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ENABLE_GMAIL", raising=False)
    servers = launcher._make_mcp_config(9222)["mcpServers"]
    assert set(servers) == {"playwright"}
    args = " ".join(servers["playwright"]["args"])
    assert "@playwright/mcp@latest" not in args and "--allow-unrestricted-file-access" not in args
    assert launcher._allowed_tools() == "mcp__playwright"
