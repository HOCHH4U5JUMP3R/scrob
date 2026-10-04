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



def test_parse_steam_xml_repairs_literal_angle_bracket_in_text():
    root = _parse_steam_xml(
        '<?xml version="1.0"?><games><game><name>Game < 3</name></game></games>'
    )
    assert root.findtext("./game/name") == "Game < 3"


def test_parse_steam_xml_recovers_from_mismatched_tags():
    root = _parse_steam_xml(
        '<?xml version="1.0"?><games><game><appID>123</appID>'
        '<name>Broken Game</wrong><hoursOnRecord>1.5</hoursOnRecord>'
        '</game></games>'
    )
    assert root.findtext("./games/game/appID") == "123"
    assert root.findtext("./games/game/name") == "Broken Game"
    assert root.findtext("./games/game/hoursOnRecord") == "1.5"
