import random

from livedoc_ot import DEL, INS, move_cursor


def test_insert_before_cursor_pushes_it():
    assert move_cursor(5, [(INS, 2, "ab")], own=False) == 7


def test_insert_at_cursor_pushes_only_for_owner():
    assert move_cursor(5, [(INS, 5, "x")], own=True) == 6
    assert move_cursor(5, [(INS, 5, "x")], own=False) == 5


def test_insert_after_cursor_does_not_move_it():
    assert move_cursor(5, [(INS, 6, "x")], own=True) == 5


def test_delete_before_cursor_shifts_it_back():
    assert move_cursor(5, [(DEL, 1, 2)], own=False) == 3


def test_delete_around_cursor_moves_it_to_range_start():
    assert move_cursor(5, [(DEL, 3, 4)], own=False) == 3  # cursor inside [3, 7)


def test_delete_ending_at_cursor_moves_it_to_range_start():
    assert move_cursor(5, [(DEL, 3, 2)], own=False) == 3


def test_delete_at_or_after_cursor_does_not_move_it():
    assert move_cursor(3, [(DEL, 3, 2)], own=False) == 3
    assert move_cursor(2, [(DEL, 3, 2)], own=False) == 2


def test_edits_move_the_cursor_in_sequence():
    # Insert at 0 pushes 1 to 2; then deleting [1, 2) pulls it back to 1.
    assert move_cursor(1, [(INS, 0, "x"), (DEL, 1, 1)], own=False) == 1


def test_cursor_stays_inside_the_text_after_random_operations():
    rng = random.Random(7)
    for _ in range(2000):
        length = rng.randint(0, 10)
        pos = rng.randint(0, length)
        edits, n = [], length
        for _ in range(rng.randint(1, 3)):
            if n == 0 or rng.random() < 0.5:
                p = rng.randint(0, n)
                t = "x" * rng.randint(1, 3)
                edits.append((INS, p, t))
                n += len(t)
            else:
                p = rng.randint(0, n - 1)
                k = rng.randint(1, n - p)
                edits.append((DEL, p, k))
                n -= k
        moved = move_cursor(pos, edits, own=rng.random() < 0.5)
        assert 0 <= moved <= n
