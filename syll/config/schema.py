"""Configuration schema using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class WhatsAppConfig(BaseModel):
    """WhatsApp channel configuration."""
    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(BaseModel):
    """Telegram channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""
    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids


class DiscordConfig(BaseModel):
    """Discord channel configuration."""
    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT


class ChannelsConfig(BaseModel):
    """Configuration for chat channels."""
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class AgentDefaults(BaseModel):
    """Default agent configuration (non-model settings)."""
    workspace: str = "~/.syll/workspace"
    max_tokens: int = 8192
    temperature: float = 0.7
    max_tool_iterations: int = 20

    class Config:
        extra = "ignore"


class AgentsConfig(BaseModel):
    """Agent configuration."""
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


# ── Purpose-based model endpoints ────────��─────────────────────────────


class ModelEndpoint(BaseModel):
    """A model endpoint: model name + credentials."""
    model: str = ""
    api_key: str = ""
    api_base: str | None = None

    @property
    def litellm_model(self) -> str:
        """Return model name with correct litellm routing prefix.

        When api_base points to OpenRouter (or api_key starts with sk-or-),
        litellm needs the ``openrouter/`` prefix so it routes through
        OpenRouter instead of the native provider.
        """
        if not self.model:
            return self.model
        is_openrouter = (
            (self.api_base and "openrouter" in self.api_base.lower())
            or (self.api_key and self.api_key.startswith("sk-or-"))
        )
        if is_openrouter and not self.model.startswith("openrouter/"):
            return f"openrouter/{self.model}"
        return self.model


class ModelsConfig(BaseModel):
    """Model endpoints organized by purpose.

    Each purpose can specify its own model/api_key/api_base.
    When empty, the fallback chain is:
      trace → planner → chat
      actor → chat (no fallback; actor is usually a different model)
      stt   → (no fallback; separate concern)
    """
    chat: ModelEndpoint = Field(
        default_factory=lambda: ModelEndpoint(model="anthropic/claude-sonnet-4-20250514"),
        description="Main chat agent",
    )
    planner: ModelEndpoint = Field(
        default_factory=ModelEndpoint,
        description="GUI planner (empty = use chat)",
    )
    actor: ModelEndpoint = Field(
        default_factory=ModelEndpoint,
        description="GUI actor / UI-TARS",
    )
    trace: ModelEndpoint = Field(
        default_factory=ModelEndpoint,
        description="Trace generation (empty = use planner → chat)",
    )
    stt: ModelEndpoint = Field(
        default_factory=ModelEndpoint,
        description="Speech-to-text (e.g. Groq Whisper)",
    )


# ── Voice (ASR + TTS) ──────────────────────────────────────────────────


class VoiceProviderConfig(BaseModel):
    """Credentials for a single voice provider (ASR or TTS).

    ``provider`` selects the backend: ``"volcengine"`` for Doubao's
    bigmodel voice APIs, ``"groq"`` for Whisper (ASR only), or empty to
    disable this side. Volcengine uses ``appid`` / ``access_token``
    with an optional ``resource_id`` / ``cluster``.

    ``voice_resources`` (TTS only) overrides the builtin voice→resource_id
    catalog. Write it as ``voiceResources`` in config.json. Example:
    ``{"zh_female_shuangkuaisisi_moon_bigtts": "volc.service_type.10029"}``.
    """
    provider: str = ""
    appid: str = ""
    access_token: str = ""
    resource_id: str = ""
    cluster: str = ""
    voice_resources: dict[str, str] = Field(default_factory=dict)


class VoiceConfig(BaseModel):
    """Voice feature configuration.

    When ``enabled`` is true the web app initializes
    ``app.state.asr_provider`` (used by ``POST /api/v1/voice/asr``) and
    the agent loop registers the ``speak`` tool — both only when their
    respective provider section is populated.
    """
    enabled: bool = True
    asr: VoiceProviderConfig = Field(default_factory=VoiceProviderConfig)
    tts: VoiceProviderConfig = Field(default_factory=VoiceProviderConfig)
    default_speaker: str = "zh_female_vv_uranus_bigtts"
    default_format: str = "mp3"


# ── Gateway ────────────────────────────────────────────────────────────


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""
    host: str = "0.0.0.0"
    port: int = 18790


# ── Tools ──────────────────────────────────────────────────────────────


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""
    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(BaseModel):
    """Web tools configuration."""
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(BaseModel):
    """Shell exec tool configuration."""
    timeout: int = 60


class GuiConfig(BaseModel):
    """GUI agent configuration."""
    enabled: bool = False
    executor: str = "pyautogui"  # pyautogui or cliclick
    click_backend: str = "auto"  # auto | quartz | pynput | pyautogui
    mac_click_style: str = "auto"  # auto | click | down_up
    preflight_permissions: bool = True
    max_steps: int = 15
    confirm_destructive: bool = True
    execution_mode: str = "planner"  # "icl" | "planner"
    selected_screen: int = 0  # Monitor index (0 = primary)

    class Config:
        extra = "ignore"


class ToolsConfig(BaseModel):
    """Tools configuration."""
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory


class IdentityConfig(BaseModel):
    """Agent and user identity used for template substitution in prompts
    and for delivering proactive rituals to the user's primary channel."""
    ghost_name: str = Field(
        default="Syll",
        description="The ghost's preferred self-name. Substituted into prompts as {{ghost_name}}.",
    )
    user_name: str = Field(
        default="",
        description="How the user wants to be addressed. Empty means no preference.",
    )
    primary_channel: str = Field(
        default="feishu",
        description="Channel used for proactive rituals. Must match an enabled channel.",
    )
    primary_chat_id: str = Field(
        default="",
        description="Target chat/user id for proactive rituals. Empty disables ritual installation.",
    )
    rituals_enabled: bool = Field(
        default=True,
        description="Master switch for proactive ritual cron jobs. Hot-reloaded via /api/v1/identity.",
    )


# ── Root config ────────────────────────────────────────────────────────


class Config(BaseSettings):
    """Root configuration for Syll."""
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def resolve_endpoint(self, purpose: str) -> ModelEndpoint:
        """Resolve model endpoint for a purpose with fallback chain.

        Fallback: trace → planner → chat, actor → chat, stt → (none).
        When a sub-endpoint has a model but no api_key, inherit credentials
        from the fallback chain (typically chat).
        """
        endpoint = getattr(self.models, purpose, None)
        if endpoint and endpoint.model:
            # If endpoint has model but no api_key, inherit from fallback
            if not endpoint.api_key and purpose != "chat":
                fallback = self._fallback_endpoint(purpose)
                if fallback and fallback.api_key:
                    return ModelEndpoint(
                        model=endpoint.model,
                        api_key=fallback.api_key,
                        api_base=endpoint.api_base or fallback.api_base,
                    )
            return endpoint
        # Fallback chain
        if purpose == "trace":
            return self.resolve_endpoint("planner")
        if purpose in ("planner", "actor"):
            return self.resolve_endpoint("chat")
        return self.models.chat

    def _fallback_endpoint(self, purpose: str) -> ModelEndpoint | None:
        """Get the fallback endpoint for a purpose (without api_key merging)."""
        if purpose == "trace":
            ep = self.models.planner
            if ep.model and ep.api_key:
                return ep
            return self.models.chat
        if purpose in ("planner", "actor"):
            return self.models.chat
        return None

    def get_api_key(self, model_or_purpose: str | None = None) -> str | None:
        """Get API key — accepts a purpose name or model string.

        Purpose names: chat, planner, actor, trace, stt.
        For backward compat, also accepts model strings (matched against chat endpoint).
        """
        # Check if it's a known purpose name
        if model_or_purpose in ("chat", "planner", "actor", "trace", "stt"):
            ep = self.resolve_endpoint(model_or_purpose)
            return ep.api_key or None
        # Default: return chat endpoint key
        return self.models.chat.api_key or None

    def get_api_base(self, model_or_purpose: str | None = None) -> str | None:
        """Get API base URL — accepts a purpose name or model string."""
        if model_or_purpose in ("chat", "planner", "actor", "trace", "stt"):
            ep = self.resolve_endpoint(model_or_purpose)
            return ep.api_base
        return self.models.chat.api_base

    class Config:
        env_prefix = "SYLL_"
        env_nested_delimiter = "__"
        extra = "ignore"
