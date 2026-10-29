from koten.config import HddNotConnectedError, load_settings


def _settings(tmp_path, hdd_root):
    p = tmp_path / "settings.toml"
    p.write_text(f'[paths]\nhdd_root = "{hdd_root}"\nssd_root = "{tmp_path / "ssd"}"\n', "utf-8")
    return load_settings(p)


def test_japanese_hdd_root(tmp_path):
    card = tmp_path / "SDカード"
    card.mkdir()
    s = _settings(tmp_path, card / "古典×悩み")
    # HDD はつながっていても koten のフォルダが無ければ止める（引っ越しの前に空で始めないように）
    try:
        s.require_hdd()
    except HddNotConnectedError as e:
        assert "hdd-copy" in str(e)
    else:
        raise AssertionError("フォルダが無いのに通った")
    (card / "古典×悩み").mkdir()
    root = s.require_hdd()
    assert root == card / "古典×悩み" and root.is_dir()


def test_card_not_inserted(tmp_path):
    s = _settings(tmp_path, tmp_path / "SDカード" / "古典×悩み")
    try:
        s.require_hdd()
    except HddNotConnectedError as e:
        assert "SDカード" in str(e)
    else:
        raise AssertionError("未接続なのに止まらなかった")
