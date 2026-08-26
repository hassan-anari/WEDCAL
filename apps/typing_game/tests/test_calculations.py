from apps.game_config.services import TypingBracket, TypingConfig
from apps.typing_game.calculations import accuracy_bps, calculate_reward, calculate_wpm


def policy() -> TypingConfig:
    return TypingConfig(
        True,
        5,
        "UTC",
        30,
        True,
        8500,
        10,
        (TypingBracket(8500, 10), TypingBracket(10000, 50)),
        (TypingBracket(10, 5), TypingBracket(100, 40)),
        90,
        160,
        220,
    )


def test_accuracy_is_unicode_normalized_integer_edit_distance() -> None:
    assert accuracy_bps("café", "cafe\u0301") == 10000
    assert accuracy_bps("abcd", "abxd") == 7500
    assert accuracy_bps("abcd", "abcd!") == 8000
    assert accuracy_bps("abcd", "abc") == 7500


def test_wpm_uses_target_characters_and_server_elapsed_milliseconds() -> None:
    assert calculate_wpm(25, 5000) == 60


def test_reward_uses_highest_matching_integer_brackets() -> None:
    assert calculate_reward(9999, 99, policy()) == 15
    assert calculate_reward(10000, 100, policy()) == 90
