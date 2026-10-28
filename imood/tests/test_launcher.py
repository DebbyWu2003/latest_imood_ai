"""Launcher plan test (no services started): which services each MODE needs."""
from imood import launcher


def names(mode, cfg=None):
    return [s.name for s in launcher.build_plan(mode, cfg=cfg or {"emotion": {"enabled": True}}, env={})]


def main():
    assert names("vendor") == ["livetalking"], names("vendor")
    assert names("full") == ["livetalking", "emotion", "voice"], names("full")
    assert names("full", {"emotion": {"enabled": False}}) == ["livetalking", "voice"]
    vendor = launcher.build_plan("vendor", cfg={}, env={})[0]
    assert vendor.argv[-1].endswith("scripts/wsl/start_step12_v2.sh"), "vendor mode starts through start_step12_v2.sh"
    assert vendor.env == {"LEGACY_VOICE": "0"}
    try:
        launcher.build_plan("nope", cfg={}, env={})
        raise AssertionError("bad mode accepted")
    except ValueError:
        pass
    print("ALL PASS")


if __name__ == "__main__":
    main()
