import unicodedata

from apps.game_config.services import TypingConfig


def levenshtein(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def accuracy_bps(target: str, submitted: str) -> int:
    target = unicodedata.normalize("NFC", target)
    submitted = unicodedata.normalize("NFC", submitted)
    denominator = max(len(target), len(submitted), 1)
    return max(denominator - levenshtein(target, submitted), 0) * 10000 // denominator


def calculate_wpm(target_character_count: int, elapsed_milliseconds: int) -> int:
    if elapsed_milliseconds <= 0:
        raise ValueError("Elapsed time must be positive")
    return target_character_count * 12000 // elapsed_milliseconds


def calculate_reward(accuracy: int, wpm: int, policy: TypingConfig) -> int:
    accuracy_points = max(
        (x.points for x in policy.accuracy_brackets if accuracy >= x.threshold), default=0
    )
    wpm_points = max((x.points for x in policy.wpm_brackets if wpm >= x.threshold), default=0)
    reward = accuracy_points + wpm_points
    if reward > policy.max_reward_per_attempt:
        raise ValueError("Calculated reward exceeds configured maximum")
    return reward


def display_score(accuracy: int | None, wpm: int | None) -> int | None:
    """Existing frontend display metric, using confirmed scoring; never a reward input."""
    if accuracy is None or wpm is None:
        return None
    # Math.round(accuracy_percent * Math.max(1, wpm)); integer-safe JS half-up semantics.
    return (accuracy * max(1, wpm) + 50) // 100
