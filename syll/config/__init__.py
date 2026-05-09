"""Configuration module for Syll."""

from syll.config.loader import get_config_path, load_config
from syll.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
