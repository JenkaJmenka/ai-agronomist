from collections import UserDict, OrderedDict
from typing import Any, Callable, Optional
_sentinel = object()


class LRUDict(UserDict):
    """
    A dictionary with Least Recently Used (LRU) eviction policy.
    Inherits from collections.UserDict and tracks usage using collections.OrderedDict.

    Attributes:
        max_size (int): The maximum number of items the dictionary can hold.
        on_evict (Optional[Callable[[Any, Any], None]]): Optional callback run when an item is evicted.
    """

    def __init__(
        self,
        max_size: int = 10,
        on_evict: Optional[Callable[[Any, Any], None]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.max_size = max_size
        self.on_evict = on_evict
        self.usage: OrderedDict[Any, bool] = OrderedDict()
        super().__init__(*args, **kwargs)

    def __getitem__(self, key: Any) -> Any:
        value = super().__getitem__(key)
        self.usage.move_to_end(key)
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self.data:
            super().__setitem__(key, value)
            self.usage.move_to_end(key)
            return

        # Evict until we are below max_size
        while len(self.data) >= self.max_size:
            if not self.usage:
                break
            lru_key, _ = self.usage.popitem(last=False)
            if lru_key in self.data:
                evicted_value = self.data.pop(lru_key)
                if self.on_evict:
                    self.on_evict(lru_key, evicted_value)

        super().__setitem__(key, value)
        self.usage[key] = True

    def __iter__(self) -> Any:
        return iter(self.usage)

    def keys(self) -> Any:
        from collections.abc import KeysView
        return KeysView(self)

    def values(self) -> Any:
        from collections.abc import ValuesView
        return ValuesView(self)

    def items(self) -> Any:
        from collections.abc import ItemsView
        return ItemsView(self)

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        if key in self.usage:
            del self.usage[key]

    def pop(self, key: Any, default: Any = _sentinel) -> Any:
        if key in self.usage:
            del self.usage[key]
        if default is _sentinel:
            return self.data.pop(key)
        return self.data.pop(key, default)

    def popitem(self) -> tuple[Any, Any]:
        if not self.data:
            raise KeyError("popitem(): dictionary is empty")
        key, value = self.data.popitem()
        if key in self.usage:
            del self.usage[key]
        return key, value

    def clear(self) -> None:
        super().clear()
        self.usage.clear()

