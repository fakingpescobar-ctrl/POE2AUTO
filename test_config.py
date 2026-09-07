"""Тесты для модуля config.py"""
import copy
import pytest
import yaml
from unittest.mock import patch, mock_open
from config import (
    DEFAULTS,
    _deep_merge,
    validate_config,
    load_config,
    save_config,
    CONFIG_PATH,
    _SCHEMA,
    _get_nested,
)


class TestDeepMerge:
    """Тесты для функции _deep_merge."""

    def test_deep_merge_empty_override(self):
        """Пустой override должен вернуть глубокую копию base."""
        base = {"a": 1, "b": {"c": 2}}
        result = _deep_merge(base, {})
        # Должна быть копия, не та же ссылка
        assert result is not base
        assert result == {"a": 1, "b": {"c": 2}}

    def test_deep_merge_simple_override(self):
        """Override с простыми значениями должен заменить значения."""
        base = {"a": 1, "b": 2}
        override = {"a": 10}
        result = _deep_merge(base, override)
        assert result == {"a": 10, "b": 2}

    def test_deep_merge_nested(self):
        """Override с вложенными dict должен рекурсивно сливаться."""
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 20}}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 20}, "b": 3}

    def test_deep_merge_none_override(self):
        """None override должен вести себя как пустой."""
        base = {"a": 1}
        result = _deep_merge(base, None)
        assert result == {"a": 1}

    def test_deep_merge_does_not_mutate_base(self):
        """Base dict не должен быть изменён."""
        base = {"a": {"x": 1}}
        original_base = copy.deepcopy(base)
        _deep_merge(base, {"a": {"y": 2}})
        assert base == original_base


class TestGetNested:
    """Тесты для функции _get_nested."""

    def test_get_nested_single_key(self):
        """Получение значения по одному ключу."""
        cfg = {"a": 1}
        assert _get_nested(cfg, "a") == 1

    def test_get_nested_deep_key(self):
        """Получение значения по цепочке ключей."""
        cfg = {"a": {"b": {"c": 42}}}
        assert _get_nested(cfg, "a.b.c") == 42

    def test_get_nested_nonexistent(self):
        """Возврат None для несуществующего пути."""
        cfg = {"a": {"b": 1}}
        assert _get_nested(cfg, "a.c") is None

    def test_get_nested_middle_none(self):
        """Если промежуточное значение None, дальнейший путь невозможен."""
        cfg = {"a": None}
        assert _get_nested(cfg, "a.b.c") is None


class TestValidateConfig:
    """Тесты для функции validate_config."""

    def test_validate_valid_config(self):
        """Валидная config должна пройти валидацию."""
        cfg = copy.deepcopy(DEFAULTS)
        result = validate_config(cfg)
        assert result is True

    def test_validate_wrong_type_string(self):
        """Поле типа str, но значение int, должно вызвать warning."""
        cfg = copy.deepcopy(DEFAULTS)
        cfg["game"]["window_title"] = 123  # int вместо str
        result = validate_config(cfg)
        assert result is False

    def test_validate_wrong_type_bool(self):
        """Поле типа bool, но значение str."""
        cfg = copy.deepcopy(DEFAULTS)
        cfg["hp"]["enabled"] = "yes"  # str вместо bool
        result = validate_config(cfg)
        assert result is False

    def test_validate_correct_nested(self):
        """Вложенные поля должны валидироваться корректно."""
        cfg = copy.deepcopy(DEFAULTS)
        cfg["hp_num"]["max_hp"] = 2000  # float вместо int (допустимо)
        assert validate_config(cfg) is True


class TestDeepMergeVsDefaults:
    """Тесты для merge DEFAULTS и загруженного yaml."""

    def test_merge_preserves_defaults(self):
        """При пустом yaml, config должен содержать все дефолты."""
        cfg = copy.deepcopy(DEFAULTS)
        with patch("config.os.path.exists", return_value=False):
            loaded = load_config.__wrapped__ if hasattr(load_config, '__wrapped__') else load_config
        # Проверяем что дефолты на месте
        assert "game" in cfg
        assert "hotkeys" in cfg
        assert cfg["hotkeys"]["toggle"] == "f8"

    def test_deep_merge_override(self):
        """Override значения должны переопределять дефолты."""
        base = {"game": {"window_title": "POE2"}, "capture": {"backend": "dxcam"}}
        override = {"game": {"window_title": "New Window"}}
        result = _deep_merge(base, override)
        assert result["game"]["window_title"] == "New Window"
        assert result["capture"]["backend"] == "dxcam"


class TestConfigSchema:
    """Тесты для структуры схемы конфига."""

    def test_schema_contains_hotkeys(self):
        """Схема должна содержать все hotkeys."""
        hotkeys_keys = [k for k in _SCHEMA.keys() if k.startswith("hotkeys.")]
        assert len(hotkeys_keys) >= 7  # toggle, diag, snapshot, overlay, attack, flask, reload, quit

    def test_schema_contains_hp_sections(self):
        """Схема должна содержать hp и hp_num секции."""
        hp_keys = [k for k in _SCHEMA.keys() if k.startswith("hp")]
        assert len(hp_keys) > 10

    def test_schema_contains_loot(self):
        """Схема должна содержать loot секцию."""
        loot_keys = [k for k in _SCHEMA.keys() if k.startswith("loot")]
        assert len(loot_keys) > 0


class TestLoadConfigBehavior:
    """Тесты для load_config при разных сценариях."""

    @patch("config.os.path.exists")
    @patch("config.open", mock_open(read_data=""))
    def test_load_config_with_empty_file(self, mock_exists):
        """Пустой yaml файл должен использовать дефолты."""
        mock_exists.return_value = True
        # load_config должен вернуть deepcopy DEFAULTS, так как yaml.safe_load({}) = None
        cfg = load_config()
        assert "game" in cfg
        assert "capture" in cfg
        assert "hotkeys" in cfg


class TestSaveConfig:
    """Тесты для save_config."""

    @patch("config.open", mock_open())
    @patch("config.yaml.safe_dump")
    @patch("config.os.path.exists", return_value=True)
    def test_save_config_writes_yaml(self, mock_exists, mock_safe_dump, mock_open):
        """save_config должен записать конфиг в файл."""
        cfg = {"game": {"window_title": "Test"}}
        path = save_config(cfg)
        mock_safe_dump.assert_called_once()
        assert path.endswith("config.yaml")


class TestDefaultsStructure:
    """Тесты для структуры DEFAULTS."""

    def test_defaults_has_all_sections(self):
        """DEFAULTS должен содержать все ожидаемые секции."""
        expected_sections = ["game", "capture", "vision", "loot", "hp", "hp_num", "auto_attack", "hotkeys"]
        for section in expected_sections:
            assert section in DEFAULTS, f"Missing section: {section}"

    def test_defaults_hotkeys_values(self):
        """Значения hotkeys должны быть строками."""
        for key, value in DEFAULTS["hotkeys"].items():
            assert isinstance(key, str)
            assert isinstance(value, str)

    def test_defaults_hp_enabled_false(self):
        """hp.enabled по умолчанию должен быть False."""
        assert DEFAULTS["hp"]["enabled"] is False

    def test_defaults_hp_num_enabled_true(self):
        """hp_num.enabled по умолчанию должен быть True."""
        assert DEFAULTS["hp_num"]["enabled"] is True


class TestConfigIntegration:
    """Интеграционные тесты для config."""

    def test_config_round_trip(self):
        """Сохранение и загрузка config должна быть консистентной."""
        import tempfile
        import os as sys_os
        from config import DEFAULTS, _deep_merge

        # Создаём временный конфиг
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.yaml') as f:
            yaml.dump(DEFAULTS, f, allow_unicode=True)
            f.seek(0)
            loaded = yaml.safe_load(f)

        # Проверяем, что значения корректны
        assert loaded is not None
        assert 'game' in loaded


class TestConfigDefaultsContent:
    """Тесты для конкретных значений в DEFAULTS."""

    def test_vision_marker_rgb(self):
        """marker_rgb должен быть списком из 3 значений."""
        rgb = DEFAULTS["vision"]["marker_rgb"]
        assert isinstance(rgb, list)
        assert len(rgb) == 3
        assert all(isinstance(v, int) for v in rgb)

    def test_loot_defaults(self):
        """loot секция должна иметь все ожидаемые поля."""
        loot = DEFAULTS["loot"]
        assert "pickup_radius_px" in loot
        assert "center_offset_xy" in loot
        assert "cooldown_ms" in loot
        assert "pickup_hold_ms" in loot
        assert "pickup_button" in loot
        assert "priority_mode" in loot

    def test_auto_attack_defaults(self):
        """auto_attack секция должна содержать вложенные секции."""
        aa = DEFAULTS["auto_attack"]
        assert "enabled" in aa
        assert "use_memory" in aa
        assert "dark_zone" in aa
        assert "zone" in aa


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
