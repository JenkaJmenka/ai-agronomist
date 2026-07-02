import pytest
from src.utils.lru import LRUDict


def test_lru_dict_max_size() -> None:
    # Test that max_size is respected
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    assert len(cache) == 3
    assert list(cache.keys()) == ["a", "b", "c"]

    # Adding a 4th item should evict the oldest ("a")
    cache["d"] = 4
    assert len(cache) == 3
    assert "a" not in cache
    assert list(cache.keys()) == ["b", "c", "d"]


def test_lru_dict_access_ordering() -> None:
    # Test that accessing an item moves it to the end (MRU)
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3

    # Access "a"
    _ = cache["a"]

    # Now "b" should be the oldest (LRU), "a" is most recently used (MRU)
    cache["d"] = 4
    assert "b" not in cache
    assert "a" in cache
    assert list(cache.keys()) == ["c", "a", "d"]


def test_lru_dict_update_ordering() -> None:
    # Test that updating an existing item moves it to the end (MRU)
    # and does not cause eviction
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3

    # Update "a"
    cache["a"] = 10
    assert len(cache) == 3

    # Now "b" is the oldest, "a" is most recently used (MRU)
    cache["d"] = 4
    assert "b" not in cache
    assert "a" in cache
    assert list(cache.keys()) == ["c", "a", "d"]


def test_lru_dict_on_evict_callback() -> None:
    evicted = []

    def on_evict(key, value):
        evicted.append((key, value))

    cache = LRUDict(max_size=2, on_evict=on_evict)
    cache["a"] = 1
    cache["b"] = 2

    cache["c"] = 3
    assert evicted == [("a", 1)]

    cache["d"] = 4
    assert evicted == [("a", 1), ("b", 2)]


def test_lru_dict_pop_and_delete() -> None:
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3

    # Delete "b"
    del cache["b"]
    assert "b" not in cache
    assert list(cache.keys()) == ["a", "c"]

    # Pop "a"
    val = cache.pop("a")
    assert val == 1
    assert "a" not in cache
    assert list(cache.keys()) == ["c"]

    # Pop non-existent
    assert cache.pop("non-existent", default=None) is None


def test_lru_dict_popitem() -> None:
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    
    key, val = cache.popitem()
    assert (key, val) == ("b", 2)
    assert "b" not in cache

    cache.popitem()
    with pytest.raises(KeyError):
        cache.popitem()


def test_lru_dict_clear() -> None:
    cache = LRUDict(max_size=3)
    cache["a"] = 1
    cache["b"] = 2
    
    cache.clear()
    assert len(cache) == 0
    assert len(cache.usage) == 0
