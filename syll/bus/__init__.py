"""Message bus module for decoupled channel-agent communication."""

from syll.bus.events import InboundMessage, OutboundMessage
from syll.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
