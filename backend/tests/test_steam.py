from xml.etree import ElementTree

from core.steam import _parse_steam_xml


def test_parse_steam_xml_repairs_unescaped_ampersand():
    root = _parse_steam_xml(
        '<?xml version="1.0"?><games><game><name>Tom & Jerry</name></game></games>'
    )
    assert root.findtext("./game/name") == "Tom & Jerry"


def test_parse_steam_xml_keeps_valid_entities():
    root = _parse_steam_xml(
        '<?xml version="1.0"?><games><game><name>Tom &amp; Jerry</name></game></games>'
    )
    assert root.findtext("./game/name") == "Tom & Jerry"


def test_parse_steam_xml_removes_invalid_control_characters():
    root = _parse_steam_xml(
        '<?xml version="1.0"?><games><game><name>Game\x0bName</name></game></games>'
    )
    assert root.findtext("./game/name") == "GameName"


def test_parse_steam_xml_removes_utf8_bom():
    root = _parse_steam_xml(
        '\\ufeff<?xml version="1.0"?><games><game><name>Steam Game</name></game></games>'
    )
    assert root.findtext("./game/name") == "Steam Game"
