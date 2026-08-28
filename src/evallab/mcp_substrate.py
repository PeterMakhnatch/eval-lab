"""Shared FastMCP multi-container task-authoring substrate and runtime middleware.

Grounding: Architecture PR #265 (research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)

Provides:
- Standard FastMCP streamable-HTTP sidecar topology generation & validation matching workbench-v2.
- Zero-egress internal bridge (internal: true), task-local named volume (main-RO / sidecar-RW).
- Standard MCP protocol compliant JSON-RPC 2.0 endpoint (/mcp) supporting initialize (2024-11-05), notifications/initialized, tools/list, and tools/call returning standard CallToolResult ({content: [{type: "text", text: ...}], isError: ...}).
- Offline hash-locked wheel dependency packaging manifest for sidecars (`fastmcp` and all transitive deps strictly pinned with verified sha256 hashes).
- Code generation for `fastmcp.FastMCP` application sidecars with customizable tool execution bodies, distractor handling, and dynamic operation registries.
- In-process MCP streamable-HTTP sidecar runtime for test execution and offline sandboxing.
- Deterministic Fault Interceptor middleware operating over FaultInjectionRecord contracts.
- Deterministic state journal / event ledger logging to /app/output or specified evidence path.
- Invariant ground-truth separation (purges solutions/oracles from agent containers).
- Substrate version & comprehensive digest computation (including execution_body and metadata).
"""

from __future__ import annotations

import http.server
import json
import logging
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.benchmark_program_contracts import (
    FaultClass,
    FaultInjectionRecord,
    canonical_bytes,
    canonical_json,
    compute_sha256,
)

logger = logging.getLogger(__name__)

MCP_SUBSTRATE_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SIDECAR_SERVICE = "mcp-service"
DEFAULT_VOLUME_NAME = "evidence-volume"
DEFAULT_VOLUME_MOUNT = "/app/output"
DEFAULT_MCP_PORT = 8080

# Pinned offline hash-locked sidecar runtime requirements specification (every package pinned with exact sha256 hashes)
FASTMCP_SIDECAR_REQUIREMENTS_TXT = """# Pinned FastMCP streamable-HTTP sidecar dependencies with strict hash locking
annotated-doc==0.0.5 --hash=sha256:117bac03a25ede5df5440e855b32d556049ca169ead221505badf432fed4b101 --hash=sha256:c7e58ce09192557605d8bbd92836d7e1d520ac9580096042c0bfd197efacf1bb
annotated-types==0.7.0 --hash=sha256:1f02e8b43a8fbbc3f3e0d4f0f4bfc8131bcb4eebe8849b8e5c773f3a1c582a53 --hash=sha256:aff07c09a53a08bc8cfccb9c85b05f1aa9a2a6f23728d790723543408344ce89
anyio==4.14.2 --hash=sha256:9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494 --hash=sha256:cfa139f3ed1a23ee8f88a145ddb5ac7605b8bbfd8592baacd7ce3d8bb4313c7f
certifi==2026.7.22 --hash=sha256:62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775 --hash=sha256:741e2c3b351ddf169a738da9f2c048608ff7f2c5cc02f1ebc6b118bb090d5d55
click==8.5.0 --hash=sha256:255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360 --hash=sha256:ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34
fastmcp==0.4.1 --hash=sha256:664b42c376fb89ec90a50c9433f5a1f4d24f36696d6c41b024b427ae545f9619 --hash=sha256:713ad3b8e4e04841c9e2f3ca022b053adb89a286ceffad0d69ae7b56f31cbe64
h11==0.16.0 --hash=sha256:4e35b956cf45792e4caa5885e69fba00bdbc6ffafbfa020300e549b208ee5ff1 --hash=sha256:63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86
httpcore==1.0.9 --hash=sha256:2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55 --hash=sha256:6e34463af53fd2ab5d807f399a9b45ea31c3dfa2276f15a2c3f00afff6e176e8
httpx==0.28.1 --hash=sha256:75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc --hash=sha256:d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad
httpx-sse==0.4.3 --hash=sha256:0ac1c9fe3c0afad2e0ebb25a934a59f4c7823b60792691f779fad2c5568830fc --hash=sha256:9b1ed0127459a66014aec3c56bebd93da3c1bc8bb6618c8082039a44889a755d
idna==3.19 --hash=sha256:5e0811a4383b21dc5838069f801c4fb62113b7447663d2530d2bd6e77b49bf15 --hash=sha256:815e7be7a7806d54abb586dc943addc79e8b2ee16915059658cbeff4b1b43bf4
markdown-it-py==4.2.0 --hash=sha256:04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49 --hash=sha256:9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a
mcp==1.3.0 --hash=sha256:2829d67ce339a249f803f22eba5e90385eafcac45c94b00cab6cef7e8f217211 --hash=sha256:f409ae4482ce9d53e7ac03f3f7808bcab735bdfc0fba937453782efb43882d45
mdurl==0.1.2 --hash=sha256:84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8 --hash=sha256:bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba
pydantic==2.10.6 --hash=sha256:427d664bf0b8a2b34ff5dd0f5a18df00591adcee7198fbd71981054cef37b584 --hash=sha256:ca5daa827cce33de7a42be142548b0096bf05a7e7b365aebfa5f8eeec7128236
pydantic-core==2.27.2 --hash=sha256:00bad2484fa6bda1e216e7345a798bd37c68fb2d97558edd584942aa41b7d278 --hash=sha256:0296abcb83a797db256b773f45773da397da75a08f5fcaef41f2044adec05f50 --hash=sha256:03d0f86ea3184a12f41a2d23f7ccb79cdb5a18e06993f8a45baa8dfec746f0e9 --hash=sha256:044a50963a614ecfae59bb1eaf7ea7efc4bc62f49ed594e18fa1e5d953c40e9f --hash=sha256:05e3a55d124407fffba0dd6b0c0cd056d10e983ceb4e5dbd10dda135c31071d6 --hash=sha256:08e125dbdc505fa69ca7d9c499639ab6407cfa909214d500897d02afb816e7cc --hash=sha256:097830ed52fd9e427942ff3b9bc17fab52913b2f50f2880dc4a5611446606a54 --hash=sha256:0d1e85068e818c73e048fe28cfc769040bb1f475524f4745a5dc621f75ac7630 --hash=sha256:0d75070718e369e452075a6017fbf187f788e17ed67a3abd47fa934d001863d9 --hash=sha256:14d4a5c49d2f009d62a2a7140d3064f686d17a5d1a268bc641954ba181880236 --hash=sha256:172fce187655fece0c90d90a678424b013f8fbb0ca8b036ac266749c09438cb7 --hash=sha256:18a101c168e4e092ab40dbc2503bdc0f62010e95d292b27827871dc85450d7ee --hash=sha256:1a4207639fb02ec2dbb76227d7c751a20b1a6b4bc52850568e52260cae64ca3b --hash=sha256:1c1fd185014191700554795c99b347d64f2bb637966c4cfc16998a0ca700d048 --hash=sha256:1ebaf1d0481914d004a573394f4be3a7616334be70261007e47c2a6fe7e50130 --hash=sha256:220f892729375e2d736b97d0e51466252ad84c51857d4d15f5e9692f9ef12be4 --hash=sha256:251136cdad0cb722e93732cb45ca5299fb56e1344a833640bf93b2803f8d1bfd --hash=sha256:26f0d68d4b235a2bae0c3fc585c585b4ecc51382db0e3ba402a22cbc440915e4 --hash=sha256:26f32e0adf166a84d0cb63be85c562ca8a6fa8de28e5f0d92250c6b7e9e2aff7 --hash=sha256:280d219beebb0752699480fe8f1dc61ab6615c2046d76b7ab7ee38858de0a4e7 --hash=sha256:28ccb213807e037460326424ceb8b5245acb88f32f3d2777427476e1b32c48c4 --hash=sha256:2bf14caea37e91198329b828eae1618c068dfb8ef17bb33287a7ad4b61ac314e --hash=sha256:2d367ca20b2f14095a8f4fa1210f5a7b78b8a20009ecced6b12818f455b1e9fa --hash=sha256:30c5f68ded0c36466acede341551106821043e9afaad516adfb6e8fa80a4e6a6 --hash=sha256:337b443af21d488716f8d0b6164de833e788aa6bd7e3a39c005febc1284f4962 --hash=sha256:3911ac9284cd8a1792d3cb26a2da18f3ca26c6908cc434a18f730dc0db7bfa3b --hash=sha256:3d591580c34f4d731592f0e9fe40f9cc1b430d297eecc70b962e93c5c668f15f --hash=sha256:3de3ce3c9ddc8bbd88f6e0e304dea0e66d843ec9de1b0042b0911c1663ffd474 --hash=sha256:3de9961f2a346257caf0aa508a4da705467f53778e9ef6fe744c038119737ef5 --hash=sha256:40d02e7d45c9f8af700f3452f329ead92da4c5f4317ca9b896de7ce7199ea459 --hash=sha256:42c5f762659e47fdb7b16956c71598292f60a03aa92f8b6351504359dbdba6cf --hash=sha256:47956ae78b6422cbd46f772f1746799cbb862de838fd8d1fbd34a82e05b0983a --hash=sha256:491a2b73db93fab69731eaee494f320faa4e093dbed776be1a829c2eb222c34c --hash=sha256:4c9775e339e42e79ec99c441d9730fccf07414af63eac2f0e48e08fd38a64d76 --hash=sha256:50a68f3e3819077be2c98110c1f9dcb3817e93f267ba80a2c05bb4f8799e2ff4 --hash=sha256:519f29f5213271eeeeb3093f662ba2fd512b91c5f188f3bb7b27bc5973816934 --hash=sha256:521eb9b7f036c9b6187f0b47318ab0d7ca14bd87f776240b90b21c1f4f149320 --hash=sha256:57762139821c31847cfb2df63c12f725788bd9f04bc2fb392790959b8f70f118 --hash=sha256:5e4f4bb20d75e9325cc9696c6802657b58bc1dbbe3022f32cc2b2b632c3fbb96 --hash=sha256:5e68c4446fe0810e959cdff46ab0a41ce2f2c86d227d96dc3847af0ba7def306 --hash=sha256:669e193c1c576a58f132e3158f9dfa9662969edb1a250c54d8fa52590045f046 --hash=sha256:688d3fd9fcb71f41c4c015c023d12a79d1c4c0732ec9eb35d96e3388a120dcf3 --hash=sha256:6fb4aadc0b9a0c063206846d603b92030eb6f03069151a625667f982887153e2 --hash=sha256:7041c36f5680c6e0f08d922aed302e98b3745d97fe1589db0a3eebf6624523af --hash=sha256:71b24c7d61131bb83df10cc7e687433609963a944ccf45190cfc21e0887b08c9 --hash=sha256:7969e133a6f183be60e9f6f56bfae753585680f3b7307a8e555a948d443cc05a --hash=sha256:7a66efda2387de898c8f38c0cf7f14fca0b51a8ef0b24bfea5849f1b3c95af27 --hash=sha256:7d0c8399fcc1848491f00e0314bd59fb34a9c008761bcb422a057670c3f65e35 --hash=sha256:7d14bd329640e63852364c306f4d23eb744e0f8193148d4044dd3dacdaacbd8b --hash=sha256:7e17b560be3c98a8e3aa66ce828bdebb9e9ac6ad5466fba92eb74c4c95cb1151 --hash=sha256:8083d4e875ebe0b864ffef72a4304827015cff328a1be6e22cc850753bfb122b --hash=sha256:82f91663004eb8ed30ff478d77c4d1179b3563df6cdb15c0817cd1cdaf34d154 --hash=sha256:82f986faf4e644ffc189a7f1aafc86e46ef70372bb153e7001e8afccc6e54133 --hash=sha256:83097677b8e3bd7eaa6775720ec8e0405f1575015a463285a92bfdfe254529ef --hash=sha256:85210c4d99a0114f5a9481b44560d7d1e35e32cc5634c656bc48e590b669b145 --hash=sha256:8c19d1ea0673cd13cc2f872f6c9ab42acc4e4f492a7ca9d3795ce2b112dd7e15 --hash=sha256:8d9b3388db186ba0c099a6d20f0604a44eabdeef1777ddd94786cdae158729e4 --hash=sha256:8e10c99ef58cfdf2a66fc15d66b16c4a04f62bca39db589ae8cba08bc55331bc --hash=sha256:953101387ecf2f5652883208769a79e48db18c6df442568a0b5ccd8c2723abee --hash=sha256:9c3ed807c7b91de05e63930188f19e921d1fe90de6b4f5cd43ee7fcc3525cb8c --hash=sha256:9e0c8cfefa0ef83b4da9588448b6d8d2a2bf1a53c3f1ae5fca39eb3061e2f0b0 --hash=sha256:9fdbe7629b996647b99c01b37f11170a57ae675375b14b8c13b8518b8320ced5 --hash=sha256:a0fcd29cd6b4e74fe8ddd2c90330fd8edf2e30cb52acda47f06dd615ae72da57 --hash=sha256:ac4dbfd1691affb8f48c2c13241a2e3b60ff23247cbcf981759c768b6633cf8b --hash=sha256:b0cb791f5b45307caae8810c2023a184c74605ec3bcbb67d13846c28ff731ff8 --hash=sha256:ba5dd002f88b78a4215ed2f8ddbdf85e8513382820ba15ad5ad8955ce0ca19a1 --hash=sha256:bca101c00bff0adb45a833f8451b9105d9df18accb8743b08107d7ada14bd7da --hash=sha256:bd8086fa684c4775c27f03f062cbb9eaa6e17f064307e86b21b9e0abc9c0f02e --hash=sha256:bec317a27290e2537f922639cafd54990551725fc844249e64c523301d0822fc --hash=sha256:c10eb4f1659290b523af58fa7cffb452a61ad6ae5613404519aee4bfbf1df993 --hash=sha256:c33939a82924da9ed65dab5a65d427205a73181d8098e79b6b426bdf8ad4e656 --hash=sha256:c61709a844acc6bf0b7dce7daae75195a10aac96a596ea1b776996414791ede4 --hash=sha256:c70c26d2c99f78b125a3459f8afe1aed4d9687c24fd677c6a4436bc042e50d6c --hash=sha256:c817e2b40aba42bac6f457498dacabc568c3b7a986fc9ba7c8d9d260b71485fb --hash=sha256:cabb9bcb7e0d97f74df8646f34fc76fbf793b7f6dc2438517d7a9e50eee4f14d --hash=sha256:cc3f1a99a4f4f9dd1de4fe0312c114e740b5ddead65bb4102884b384c15d8bc9 --hash=sha256:ce8918cbebc8da707ba805b7fd0b382816858728ae7fe19a942080c24e5b7cd1 --hash=sha256:d2088237af596f0a524d3afc39ab3b036e8adb054ee57cbb1dcf8e09da5b29cc --hash=sha256:d262606bf386a5ba0b0af3b97f37c83d7011439e3dc1a9298f21efb292e42f1a --hash=sha256:d2d63f1215638d28221f664596b1ccb3944f6e25dd18cd3b86b0a4c408d5ebb9 --hash=sha256:d3e8d504bdd3f10835468f29008d72fc8359d95c9c415ce6e767203db6127506 --hash=sha256:d4041c0b966a84b4ae7a09832eb691a35aec90910cd2dbe7a208de59be77965b --hash=sha256:d716e2e30c6f140d7560ef1538953a5cd1a87264c737643d481f2779fc247fe1 --hash=sha256:d81d2068e1c1228a565af076598f9e7451712700b673de8f502f0334f281387d --hash=sha256:d9640b0059ff4f14d1f37321b94061c6db164fbe49b334b31643e0528d100d99 --hash=sha256:de3cd1899e2c279b140adde9357c4495ed9d47131b4a4eaff9052f23398076b3 --hash=sha256:e0fd26b16394ead34a424eecf8a31a1f5137094cabe84a1bcb10fa6ba39d3d31 --hash=sha256:e2bb4d3e5873c37bb3dd58714d4cd0b0e6238cebc4177ac8fe878f8b3aa8e74c --hash=sha256:eb026e5a4c1fee05726072337ff51d1efb6f59090b7da90d30ea58625b1ffb39 --hash=sha256:eda3f5c2a021bbc5d976107bb302e0131351c2ba54343f8a496dc8783d3d3a6a --hash=sha256:ef592d4bad47296fb11f96cd7dc898b92e795032b4894dfb4076cfccd43a9308 --hash=sha256:f141ee28a0ad2123b6611b6ceff018039df17f32ada8b534e6aa039545a3efb2 --hash=sha256:f66d89ba397d92f840f8654756196d93804278457b5fbede59598a1f9f90b228 --hash=sha256:f6f8e111843bbb0dee4cb6594cdc73e79b3329b526037ec242a3e49012495b3b --hash=sha256:fd1aea04935a508f62e0d0ef1f5ae968774a32afc306fb8545e06f5ff5cdf3ad
pydantic-settings==2.15.0 --hash=sha256:0ba092c291c94baceb5eff768aa0d56400a457585bc0175925a5a5510303da42 --hash=sha256:694b793e84f766ba76a90ebdefc01d0a9a045dab0382bee70393da93712ad117
pygments==2.21.0 --hash=sha256:2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9 --hash=sha256:610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c
python-dotenv==1.2.3 --hash=sha256:904552145e8bfed22162c09dab1c2b9b54fefa7b23ba780f4f26ca0316b0f0d9 --hash=sha256:a20a594dabeaa385725aa239d5244871c143ecb356add8a20fcf23773a6c3a35
rich==15.0.0 --hash=sha256:33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb --hash=sha256:edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36
shellingham==1.5.4 --hash=sha256:7ecfff8f2fd72616f7481040475a65b2bf8af90a56c89140852d1120324e8686 --hash=sha256:8dbca0739d487e5bd35ab3ca4b36e11c4078f3a234bfce294b0a0291363404de
sse-starlette==3.4.8 --hash=sha256:6e82314c786709a3cd9520f2285cf9fff90e181e598e8a357b0cf80f66afba0d --hash=sha256:ed89ffbb75cbf78a5fe2f2109cd584792ee7f9dfac96f791db546df8f15f3f9c
starlette==1.6.0 --hash=sha256:a86dd39d14bb45f85a3d18525215a9ef0cfd1f192ac793220e72598c90335f0c --hash=sha256:d4e3ac5e546444960c710297a3c9fc3f7ebae1b7e963f3d36173b49da535be9b
typer==0.27.2 --hash=sha256:269b7eb9d3c202ca84b4bc9618cb04ebb43d3d4d1e567e4c768607232c05f945 --hash=sha256:b3a5fc4342d5fc8fda8fc3010b1cf117e9249aab7fae800c2eff62fd3842d97d
typing-extensions==4.12.2 --hash=sha256:04e5ca0351e0f3f85c6853954072df659d0d13fac324d0072316b67d7794700d --hash=sha256:1a7ead55c7e559dd4dee8856e3a88b41225abfe1ce8df57b7c13915fe121ffb8
typing-inspection==0.4.2 --hash=sha256:4ed1cacbdc298c220f1bd249ed5287caa16f34d44ef4e9c3d0cbad5b521545e7 --hash=sha256:ba561c48a67c5958007083d386c3295464928b01faa735ab8547c5692e87f464
uvicorn==0.52.4 --hash=sha256:73acfee47a0b133c5de13d219492d62d8a31e935f4fe6e41a232451a15379f86 --hash=sha256:f86e41a149d7d05a9969337e3946a9c171c06a5d42680896daaba624aeac8da1
"""


class SubstrateError(Exception):
    """Raised when substrate configuration, validation, or runtime fails."""


@dataclass(frozen=True)
class MCPToolParameter:
    name: str
    type_name: str
    description: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "description": self.description,
            "required": self.required,
        }


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    description: str
    parameters: tuple[MCPToolParameter, ...]
    output_type: str = "object"
    is_distractor: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    execution_body: str | None = None

    def to_mcp_tool_schema(self) -> dict[str, Any]:
        """Convert to standard MCP tools/list tool schema (JSON Schema inputSchema)."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            type_mapping = {
                "int": "integer",
                "integer": "integer",
                "float": "number",
                "number": "number",
                "str": "string",
                "string": "string",
                "bool": "boolean",
                "boolean": "boolean",
                "dict": "object",
                "object": "object",
                "list": "array",
                "array": "array",
            }
            json_type = type_mapping.get(param.type_name.lower(), "string")
            properties[param.name] = {
                "type": json_type,
                "description": param.description,
            }
            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }


@dataclass
class ToolExecutionContext:
    tool_name: str
    arguments: dict[str, Any]
    call_ordinal: int
    raw_event_ordinal: int


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class FaultInterceptorMiddleware:
    """Deterministic fault interceptor operating on FaultInjectionRecord ledgers.

    Evaluates every tool call against the registered fault record. When the sequence ordinal
    matches target_canonical_event_ordinal and target_tool matches, intercepts the call
    and returns or raises the configured fault response.
    """

    def __init__(self, fault_record: FaultInjectionRecord | None = None) -> None:
        self.fault_record = fault_record
        self.injected_calls: list[dict[str, Any]] = []

    def should_intercept(self, tool_name: str, call_ordinal: int) -> bool:
        if self.fault_record is None:
            return False
        if self.fault_record.target_tool != tool_name:
            return False
        return call_ordinal == self.fault_record.target_canonical_event_ordinal

    def apply_fault(
        self, tool_name: str, arguments: dict[str, Any], call_ordinal: int
    ) -> dict[str, Any]:
        assert self.fault_record is not None
        record = self.fault_record
        fault_class = record.fault_class
        payload = record.injection_payload

        self.injected_calls.append(
            {
                "fault_id": record.fault_id,
                "fault_class": fault_class.value,
                "tool_name": tool_name,
                "call_ordinal": call_ordinal,
                "arguments": arguments,
            }
        )

        if fault_class == FaultClass.TRANSIENT_HTTP_5XX:
            return {
                "is_error": True,
                "http_status": 500,
                "error": {
                    "code": -32000,
                    "message": payload.get(
                        "message", "Internal Server Error: transient sidecar 500"
                    ),
                },
            }
        elif fault_class == FaultClass.TRANSIENT_NETWORK_TIMEOUT:
            return {
                "is_error": True,
                "http_status": 504,
                "error": {
                    "code": -32001,
                    "message": payload.get(
                        "message", "Gateway Timeout: upstream tool response timed out"
                    ),
                },
            }
        elif fault_class == FaultClass.PERSISTENT_SCHEMA_MISMATCH:
            return {
                "is_error": True,
                "http_status": 200,
                "error": {
                    "code": -32602,
                    "message": payload.get(
                        "message",
                        f"Schema mismatch for tool {tool_name}: unexpected schema mutation",
                    ),
                    "data": payload.get(
                        "data",
                        {"expected_schema": payload.get("expected_schema", "v2_signature")},
                    ),
                },
            }
        elif fault_class == FaultClass.PERSISTENT_SIGNATURE_ERROR:
            return {
                "is_error": True,
                "http_status": 200,
                "error": {
                    "code": -32602,
                    "message": payload.get(
                        "message",
                        f"Signature error in tool {tool_name}: invalid positional argument binding",
                    ),
                },
            }
        elif fault_class == FaultClass.SILENT_WRONG_PAYLOAD:
            corrupted_result = payload.get(
                "corrupted_result",
                {"value": payload.get("corrupted_value", "CORRUPTED_VALUE")},
            )
            return {
                "is_error": False,
                "http_status": 200,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(corrupted_result)}],
                    "isError": False,
                    "value": corrupted_result.get("value", corrupted_result),
                },
                "_silent_fault_injected": True,
            }
        else:
            raise SubstrateError(f"Unhandled fault class: {fault_class}")


class FastMCPRuntime:
    """In-memory FastMCP engine managing tools, execution, event ledgers, and fault middleware."""

    def __init__(
        self,
        tools: Sequence[MCPToolDefinition],
        handlers: Mapping[str, ToolHandler] | None = None,
        fault_record: FaultInjectionRecord | None = None,
        evidence_dir: Path | None = None,
        state_log_name: str = "state-journal.jsonl",
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.handlers = dict(handlers or {})
        self.fault_interceptor = FaultInterceptorMiddleware(fault_record)
        self.evidence_dir = evidence_dir
        self.state_log_name = state_log_name
        self.call_count = 0
        self.events: list[dict[str, Any]] = []
        self._prior_tool_calls: set[str] = set()

        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def register_tool(self, tool_def: MCPToolDefinition, handler: ToolHandler) -> None:
        self.tools[tool_def.name] = tool_def
        self.handlers[tool_def.name] = handler

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.to_mcp_tool_schema() for t in self.tools.values()]

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        self.call_count += 1
        ordinal = self.call_count
        call_sig = f"{tool_name}:{canonical_json(arguments)}"
        is_redundant = call_sig in self._prior_tool_calls
        self._prior_tool_calls.add(call_sig)

        if tool_name not in self.tools:
            err_resp = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": f"Method not found: unknown tool {tool_name!r}",
                },
            }
            self._log_event(
                {
                    "event_ordinal": ordinal,
                    "event_type": "tool_call_rejected",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "is_redundant": is_redundant,
                    "error": err_resp["error"],
                }
            )
            return err_resp, 200

        tool_def = self.tools[tool_name]
        missing_params = [
            p.name for p in tool_def.parameters if p.required and p.name not in arguments
        ]
        if missing_params:
            err_resp = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32602,
                    "message": f"Invalid params: missing required argument(s): {missing_params}",
                },
            }
            self._log_event(
                {
                    "event_ordinal": ordinal,
                    "event_type": "tool_call_rejected",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "is_redundant": is_redundant,
                    "schema_conforming": False,
                    "error": err_resp["error"],
                }
            )
            return err_resp, 200

        if self.fault_interceptor.should_intercept(tool_name, ordinal):
            fault_res = self.fault_interceptor.apply_fault(tool_name, arguments, ordinal)
            status_code = fault_res.get("http_status", 200)
            if fault_res.get("is_error"):
                response = {"jsonrpc": "2.0", "error": fault_res["error"]}
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_fault_injected",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "fault_class": self.fault_interceptor.fault_record.fault_class.value,  # type: ignore
                        "fault_id": self.fault_interceptor.fault_record.fault_id,  # type: ignore
                        "error": fault_res["error"],
                    }
                )
                return response, status_code
            else:
                response = {"jsonrpc": "2.0", "result": fault_res["result"]}
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_silent_fault_injected",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "fault_class": self.fault_interceptor.fault_record.fault_class.value,  # type: ignore
                        "fault_id": self.fault_interceptor.fault_record.fault_id,  # type: ignore
                        "result": fault_res["result"],
                    }
                )
                return response, status_code

        handler = self.handlers.get(tool_name)
        if handler is None:
            raw_res = {"status": "ok", "tool": tool_name, "arguments": arguments}
        else:
            try:
                raw_res = handler(arguments)
            except Exception as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": f"Tool execution failed: {exc}"},
                }
                self._log_event(
                    {
                        "event_ordinal": ordinal,
                        "event_type": "tool_call_exception",
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "is_redundant": is_redundant,
                        "error": str(exc),
                    }
                )
                return err_resp, 200

        val = raw_res.get("value", raw_res) if isinstance(raw_res, dict) else raw_res
        res_data = {
            "content": [
                {
                    "type": "text",
                    "text": (
                        json.dumps(raw_res) if isinstance(raw_res, (dict, list)) else str(raw_res)
                    ),
                }
            ],
            "isError": False,
            "value": val,
        }

        response = {"jsonrpc": "2.0", "result": res_data}
        self._log_event(
            {
                "event_ordinal": ordinal,
                "event_type": "tool_call_success",
                "tool_name": tool_name,
                "arguments": arguments,
                "is_redundant": is_redundant,
                "schema_conforming": True,
                "result": res_data,
            }
        )
        return response, 200

    def _log_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if self.evidence_dir is not None:
            log_file = self.evidence_dir / self.state_log_name
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(canonical_json(event) + "\n")


def generate_fastmcp_server_script(
    tools: Sequence[MCPToolDefinition],
    server_name: str = "eval-lab-fastmcp-sidecar",
    port: int = DEFAULT_MCP_PORT,
    evidence_path: str = "/app/output/benchmark-events.jsonl",
    op_registry_module: str | None = None,
) -> str:
    """Generate production-ready FastMCP sidecar server script with full event recording and tool execution."""
    lines = [
        '"""Generated FastMCP Streamable-HTTP sidecar server with state journal recording."""',
        "from __future__ import annotations",
        "",
        "import json",
        "from pathlib import Path",
        "import threading",
        "from typing import Any",
        "from fastmcp import FastMCP",
    ]

    if op_registry_module:
        lines.append(f"from {op_registry_module} import OP_REGISTRY")
    else:
        lines.append("OP_REGISTRY: dict[str, Any] = {}")

    lines.extend(
        [
            "",
            f'mcp = FastMCP("{server_name}")',
            f'EVIDENCE_FILE = Path("{evidence_path}")',
            "EVENT_LOCK = threading.Lock()",
            "EVENT_ORDINAL = 0",
            "",
            "def log_tool_event(tool_name: str, arguments: dict[str, Any], result: Any, is_distractor: bool = False) -> None:",
            "    global EVENT_ORDINAL",
            "    with EVENT_LOCK:",
            "        EVENT_ORDINAL += 1",
            "        EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)",
            "        event = {",
            '            "event_ordinal": EVENT_ORDINAL,',
            '            "event_type": "tool_call_success",',
            '            "tool_name": tool_name,',
            '            "arguments": arguments,',
            '            "result": result,',
            '            "is_distractor": is_distractor,',
            "        }",
            '        with open(EVIDENCE_FILE, "a", encoding="utf-8") as f:',
            '            f.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\\n")',
            "",
        ]
    )

    for tool in tools:
        param_sigs = []
        arg_dict_entries = []
        for p in tool.parameters:
            py_type = (
                p.type_name
                if p.type_name in ("int", "str", "float", "bool", "dict", "list")
                else "Any"
            )
            if not p.required:
                param_sigs.append(f"{p.name}: {py_type} | None = None")
            else:
                param_sigs.append(f"{p.name}: {py_type}")
            arg_dict_entries.append(f'"{p.name}": {p.name}')
        sig_str = ", ".join(param_sigs)
        arg_dict_str = "{" + ", ".join(arg_dict_entries) + "}"

        lines.extend(
            [
                "@mcp.tool()",
                f"def {tool.name}({sig_str}) -> dict[str, Any]:",
                f'    """{tool.description}"""',
                f"    args = {arg_dict_str}",
            ]
        )

        if tool.execution_body:
            for b_line in tool.execution_body.strip().splitlines():
                lines.append(f"    {b_line}")
        elif tool.is_distractor:
            lines.extend(
                [
                    '    res = {"status": "noop_distractor", "value": None}',
                    f'    log_tool_event("{tool.name}", args, res, is_distractor=True)',
                    "    return res",
                ]
            )
        else:
            op_kind = tool.metadata.get("op_kind", tool.name)
            lines.extend(
                [
                    f'    op_fn = OP_REGISTRY.get("{op_kind}")',
                    "    if op_fn is not None:",
                    "        val = op_fn(**args)",
                    '        res = {"status": "ok", "value": val}',
                    "    else:",
                    '        res = {"status": "ok", "tool": "' + tool.name + '", "value": args}',
                    f'    log_tool_event("{tool.name}", args, res, is_distractor=False)',
                    "    return res",
                ]
            )
        lines.append("")

    lines.extend(
        [
            'if __name__ == "__main__":',
            f'    mcp.run(transport="streamable-http", host="0.0.0.0", port={port})',
            "",
        ]
    )
    return "\n".join(lines)


def make_fastmcp_http_handler(
    runtime: FastMCPRuntime,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Create a standard HTTP request handler serving the FastMCP JSON-RPC streamable interface."""

    class FastMCPHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send_json(self, status: int, data: Any) -> None:
            body = canonical_bytes(data)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health" or parsed.path == "/":
                self._send_json(200, {"status": "ok", "version": MCP_SUBSTRATE_VERSION})
            elif parsed.path == "/events":
                body = "\n".join(canonical_json(e) for e in runtime.events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json(404, {"error": f"Path not found: {parsed.path}"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/mcp" and parsed.path != "/":
                self._send_json(404, {"error": f"Invalid endpoint: {parsed.path}, use /mcp"})
                return

            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except Exception as exc:
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    },
                )
                return

            req_id = payload.get("id")
            method = payload.get("method")
            params = payload.get("params", {})

            if method == "initialize":
                protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "logging": {},
                        },
                        "serverInfo": {
                            "name": "eval-lab-fastmcp-sidecar",
                            "version": MCP_SUBSTRATE_VERSION,
                        },
                    },
                }
                self._send_json(200, res)
            elif method == "notifications/initialized":
                if req_id is not None:
                    self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
                else:
                    self.send_response(204)
                    self.end_headers()
            elif method == "tools/list":
                tools = runtime.list_tools()
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": tools,
                    },
                }
                self._send_json(200, res)
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                call_resp, status_code = runtime.call_tool(tool_name, arguments)
                call_resp["id"] = req_id
                self._send_json(status_code, call_resp)
            else:
                if req_id is not None:
                    self._send_json(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32601,
                                "message": f"Method not implemented: {method!r}",
                            },
                        },
                    )
                else:
                    self.send_response(204)
                    self.end_headers()

    return FastMCPHandler


def compute_mcp_substrate_digest(
    topology: dict[str, Any],
    tool_defs: Sequence[MCPToolDefinition] | None = None,
) -> str:
    """Compute deterministic SHA-256 digest of the MCP substrate manifest, requirements, and full tool definitions."""
    payload: dict[str, Any] = {
        "substrate_version": MCP_SUBSTRATE_VERSION,
        "topology": topology,
        "requirements_hash": compute_sha256(FASTMCP_SIDECAR_REQUIREMENTS_TXT),
    }
    if tool_defs is not None:
        payload["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [p.to_dict() for p in t.parameters],
                "output_type": t.output_type,
                "is_distractor": t.is_distractor,
                "metadata": dict(t.metadata),
                "execution_body": t.execution_body or "",
            }
            for t in sorted(tool_defs, key=lambda x: x.name)
        ]
    return compute_sha256(payload)


def render_mcp_compose_document(
    sidecar_service: str = DEFAULT_SIDECAR_SERVICE,
    volume_name: str | None = DEFAULT_VOLUME_NAME,
    volume_mount: str = DEFAULT_VOLUME_MOUNT,
    sidecar_build_context: str = "./mcp-server",
    main_image: str = "ghcr.io/eval-lab/eval-lab-agent-base@sha256:ba5e000000000000000000000000000000000000000000000000000000000000",
) -> dict[str, Any]:
    """Render a canonical Harbor workbench-v2 Compose document structure."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", sidecar_service):
        raise SubstrateError(f"Invalid sidecar service name: {sidecar_service!r}")

    services: dict[str, Any] = {
        "main": {
            "image": main_image,
        },
        sidecar_service: {
            "build": {
                "context": sidecar_build_context,
            },
        },
    }

    volumes_section: dict[str, Any] | None = None
    if volume_name:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", volume_name):
            raise SubstrateError(f"Invalid volume name: {volume_name!r}")
        services["main"]["volumes"] = [f"{volume_name}:{volume_mount}:ro"]
        services[sidecar_service]["volumes"] = [f"{volume_name}:{volume_mount}:rw"]
        volumes_section = {volume_name: None}

    doc: dict[str, Any] = {
        "services": services,
    }
    if volumes_section is not None:
        doc["volumes"] = volumes_section

    return doc


def validate_mcp_compose_document(
    data: Any, allowed_sidecar: str = DEFAULT_SIDECAR_SERVICE
) -> tuple[bool, list[str]]:
    """Strictly validate a Compose document against Harbor workbench-v2 and zero-leakage constraints."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return False, ["Compose document must be a mapping"]

    for top_key in data:
        if top_key not in {"services", "volumes", "version"}:
            errors.append(f"Unauthorized top-level Compose key: {top_key!r}")

    services = data.get("services")
    if not isinstance(services, Mapping):
        return False, ["Compose 'services' must be a mapping"]

    if "main" not in services:
        errors.append("Compose topology must declare 'main' service")

    service_names = list(services.keys())
    if len(service_names) > 2:
        errors.append(
            f"Compose topology admits at most 2 services, got {len(service_names)}: {service_names}"
        )

    top_volumes = data.get("volumes")
    volume_name: str | None = None
    if top_volumes is not None:
        if not isinstance(top_volumes, Mapping):
            errors.append("Compose 'volumes' must be a mapping")
        elif len(top_volumes) > 1:
            errors.append(f"At most 1 volume allowed, got {len(top_volumes)}")
        elif top_volumes:
            volume_name = next(iter(top_volumes.keys()))

    for name, s_cfg in services.items():
        if not isinstance(s_cfg, Mapping):
            errors.append(f"Service {name!r} configuration must be a mapping")
            continue

        if "network_mode" in s_cfg:
            errors.append(f"Service {name!r} may not declare custom network_mode")
        if "networks" in s_cfg:
            errors.append(f"Service {name!r} may not declare custom networks")
        if "ports" in s_cfg or "expose" in s_cfg:
            errors.append(f"Service {name!r} may not publish or expose host ports")
        if "privileged" in s_cfg:
            errors.append(f"Service {name!r} may not request privileged mode")
        if "depends_on" in s_cfg:
            errors.append(f"Service {name!r} may not declare depends_on")

        if name == "main" and "environment" in s_cfg and s_cfg["environment"]:
            errors.append("main service may not declare an environment")

        mounts = s_cfg.get("volumes", [])
        if mounts:
            if not isinstance(mounts, Sequence):
                errors.append(f"Service {name!r} volumes must be a sequence")
            else:
                for m in mounts:
                    if not isinstance(m, str):
                        errors.append(f"Service {name!r} volume entry must be a string, got {m!r}")
                        continue
                    parts = m.split(":")
                    if len(parts) < 2:
                        errors.append(f"Invalid volume syntax in service {name!r}: {m!r}")
                        continue
                    v_source, v_target = parts[0], parts[1]
                    mode = parts[2] if len(parts) > 2 else "rw"
                    if v_source != volume_name:
                        errors.append(
                            f"Service {name!r} volume source {v_source!r} does not match top-level {volume_name!r}"
                        )
                    if not v_target.startswith("/"):
                        errors.append(
                            f"Service {name!r} volume target {v_target!r} must be absolute"
                        )
                    if name == "main" and mode != "ro":
                        errors.append(
                            f"main service must mount evidence volume as read-only (:ro), got {mode!r}"
                        )
                    elif name != "main" and mode != "rw":
                        errors.append(
                            f"sidecar service {name!r} must mount evidence volume as read-write (:rw), got {mode!r}"
                        )

    return len(errors) == 0, errors
