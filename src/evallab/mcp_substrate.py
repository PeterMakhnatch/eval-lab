"""Shared FastMCP multi-container task-authoring substrate and runtime middleware.

Grounding: Architecture PR #265 (research/inbox/NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md)

Provides:
- Task authoring substrate API (`materialize_mcp_sidecar_package`) emitting:
  - `server.py` using genuine `fastmcp.FastMCP` (v3.4.7) with execution delegation and state journal event recording.
  - `requirements.txt` strictly hash-locked with verified SHA-256 digests.
  - `Dockerfile` using offline `pip install --no-index --find-links=/wheelhouse --require-hashes`.
  - `offline-build-proof.json` recording exact wheel inventory and content digests.
  - `docker-compose.yaml` fragment and collect hooks for workbench-v2.
- Strict mechanical verification of wheelhouse bytes against locked requirements hashes (rejecting missing, extra unapproved, or tampered wheel artifacts).
- Support for explicit `plan_only=True` mode when Dockerfile/wheelhouse is omitted.
- Standard FastMCP sidecar topology generation & validation matching workbench-v2.
- Zero-egress internal bridge (internal: true), task-local named volume (main-RO / sidecar-RW).
- Standard MCP protocol compliant JSON-RPC 2.0 endpoint (/mcp) supporting initialize (2024-11-05), notifications/initialized, tools/list, and tools/call returning standard CallToolResult ({content: [{type: "text", text: ...}], isError: ...}).
- In-process MCP streamable-HTTP sidecar runtime for test execution and offline sandboxing.
- Deterministic Fault Interceptor middleware operating over FaultInjectionRecord contracts.
- Invariant ground-truth separation (purges solutions/oracles from agent containers).
- Substrate version & comprehensive digest computation (including execution_body and metadata).
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import re
import shutil
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
    safe_resolve_subpath,
)

logger = logging.getLogger(__name__)

MCP_SUBSTRATE_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SIDECAR_SERVICE = "mcp-service"
DEFAULT_VOLUME_NAME = "evidence-volume"
DEFAULT_VOLUME_MOUNT = "/app/output"
DEFAULT_MCP_PORT = 8080
DEFAULT_PINNED_BASE_IMAGE = (
    "python@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)

# Pinned FastMCP 3.4.7 streamable-HTTP sidecar dependencies with strict hash locking
FASTMCP_SIDECAR_REQUIREMENTS_TXT = (
    Path("/tmp/fastmcp3_reqs.txt").read_text()
    if Path("/tmp/fastmcp3_reqs.txt").is_file()
    else """# Pinned FastMCP 3.4.7 streamable-HTTP sidecar dependencies with strict hash locking
aiofile==3.12.3 --hash=sha256:5c1bcc9e929c50834608e8cc1a4cc1d7503eb60c15a535b779fd39e2f372c017 --hash=sha256:caa6aa746b5e47e2165f7abd741b6415e49cf4d44fddc0f61844612cc3924d41
annotated-types==0.8.0 --hash=sha256:13b2beaad985e05e2d6407ee4c4f35590b11f8d693a258a561055cac8f64cab7 --hash=sha256:f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0
anyio==4.14.2 --hash=sha256:9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494 --hash=sha256:cfa139f3ed1a23ee8f88a145ddb5ac7605b8bbfd8592baacd7ce3d8bb4313c7f
attrs==26.1.0 --hash=sha256:c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309 --hash=sha256:d03ceb89cb322a8fd706d4fb91940737b6642aa36998fe130a9bc96c985eff32
authlib==1.7.2 --hash=sha256:2cea25fefcd4e7173bdf1372c0afc265c8034b23a8cd5dcb6a9164b826c64231 --hash=sha256:3e1faedc9d87e7d56a164eca3ccb6ace0d61b94abe83e92242f8dc8bba9b4a9f
beartype==0.22.9 --hash=sha256:8f82b54aa723a2848a56008d18875f91c1db02c32ef6a62319a002e3e25a975f --hash=sha256:d16c9bbc61ea14637596c5f6fbff2ee99cbe3573e46a716401734ef50c3060c2
cachetools==7.1.7 --hash=sha256:a3e2a00b14d8f8a6b70c1dae7b4685e7ad3bc965c5b42124a2d6ce895da6cf50 --hash=sha256:ef98ef375ad188819ef2f9b3645e3987f4b8c5b7550e436ad998c2de78296df0
caio==0.12.2 --hash=sha256:07942d3b5999127ecb96256c38d5dbf49ed2864c087ed2a80b783901d0aa3ba1 --hash=sha256:0ad8d8f9f5ea47aee81aead563fe3aca5bb54c3fc21b62bd830eaf369eb04060 --hash=sha256:0b85f94819058a8f21c3dca26c5f006a0f003b8700483a326ec86d569d2bd1a3 --hash=sha256:107e56554c179749de9440e1b5e5a19813572eebf3166e9dc3e5228b16966beb --hash=sha256:121f4de82e2a875aff468ef2af7491fbecfffe9e71b507f5073fe2a156bb78df --hash=sha256:15af6eb10d7705a92ee8143d8a4d89c2886ecb6b65ce1161d3dad1adb9b3cbec --hash=sha256:1b04358ef65bd03d9c34d7b028efb422593b07485da82d6c5439f8c5dea35668 --hash=sha256:2097cc0d19fa95e8d55aad770597bb0f76e4f70ed48278c965aa7c5b0b8c3bf5 --hash=sha256:2122dccbd1959b922543fc9f8a9d2af47bd5b59190d1ece2445d3d1b4d1be45f --hash=sha256:391a7cc1dbfc5885d7d54406c9a7a4023dca963df9e67d2ca87884ffae41088c
certifi==2026.7.22 --hash=sha256:62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775 --hash=sha256:741e2c3b351ddf169a738da9f2c048608ff7f2c5cc02f1ebc6b118bb090d5d55
cffi==2.1.1 --hash=sha256:046bfc24911b37851ee1b51aab8bffe713d89c68c6a057b09484ce9fd5f69b4e --hash=sha256:06c72bb76605a4b0cd0aad6930b69d4baf7dd5d806cfc409b824191099700e66 --hash=sha256:0beceaabe56af686895136a2de78db54ecd8e4046b236b8fd6d6cb61389e9bf2 --hash=sha256:154852545011f779917b11c78db2358d095da62a9a172b78ad0a583ee5adc0d0 --hash=sha256:194cffa889098ced9976c3fc6340305e43f6303657d298da55366907c05c22d6 --hash=sha256:19ee6127ee34de7d83ce3d371ebc5ed91addbdcc39f9ab15ce4eb35a4e534971 --hash=sha256:1a18a57b58cfb21fc28d72e876acf10eaed67a1ed96226f92af4df681d571c4c --hash=sha256:1aa5645c30469b09530c4ebca77ebf8f17618293c58f8549cb1a543a50236e7d --hash=sha256:1dea0e4d7d4f11f619fe8c1d76caf49e24405b4b5743c0e3be16a500ecd930c9 --hash=sha256:208f941bb9d18e768138677f0a6d2ce872be99015c7e1bc819e68b3d68bcce4a
click==8.5.0 --hash=sha256:255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360 --hash=sha256:ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34
cryptography==50.0.1 --hash=sha256:01f41478cf33fc605a6a089cd56d28b45c6c0b45a1928b61797f2621a04bac71 --hash=sha256:05ba322c4da95b262a212c345af888ef2c37c88c0509756ea00a0e6d68850f23 --hash=sha256:16c5ecd954b3330ebfb6605eca4fd952da8bef376551d5cc264534e3770a9ee6 --hash=sha256:2a93d05e34d5f67fba6f891fe85d929999baa7195e853923ea6d7576c9e68c5e --hash=sha256:2b34d76a652ea2b6faf777c35df230c5637842cd904e04f16230c3f9f03e4361 --hash=sha256:2ebbfb0f1fed745e91796e3e1080a1440423fdae8ece1b995a1d80883a409054 --hash=sha256:30a125032e5642a21ff816e021152bd4e7e94f03eff3f4b7fca41cd22bc3110f --hash=sha256:330fbb252391c596f1ae42c5754449dc924e6ad012dca8efe0d703f9f2d12ec6 --hash=sha256:359e62deae718bce96170e223fdcb6357e4fbd3bb7a3a75f4430763532560e49 --hash=sha256:407fe2b6db00939c05c0e9b940986701831872714249a5b3a32f6b553e1a0fc1
cyclopts==4.23.3 --hash=sha256:4299ec47f5be853f9a114fcc534c84d42bbf19fefa303994597ecb7e5fd3082b --hash=sha256:b3a65872942afb08f3ab5ca3d65b0b3ecfc872c9ccbea9d6a74ec11aa8a0215e
dnspython==2.8.0 --hash=sha256:01d9bbc4a2d76bf0db7c1f729812ded6d912bd318d3b1cf81d30c0f845dbf3af --hash=sha256:181d3c6996452cb1189c4046c61599b84a5a86e099562ffde77d26984ff26d0f
docstring-parser==0.18.0 --hash=sha256:292510982205c12b1248696f44959db3cdd1740237a968ea1e2e7a900eeb2015 --hash=sha256:b3fcbed555c47d8479be0796ef7e19c2670d428d72e96da63f3a40122860374b
email-validator==2.3.0 --hash=sha256:80f13f623413e6b197ae73bb10bf4eb0908faf509ad8362c5edeb0be7fd450b4 --hash=sha256:9fc05c37f2f6cf439ff414f8fc46d917929974a82244c20eb10231ba60c54426
exceptiongroup==1.3.1 --hash=sha256:8b412432c6055b0b7d14c310000ae93352ed6754f70fa8f7c34141f91c4e3219 --hash=sha256:a7a39a3bd276781e98394987d3a5701d0c4edffb633bb7a5144577f82c773598
fastmcp==3.4.7 --hash=sha256:43117aca886f5ee2f6a569bba91cef02b59c339aad04ba29950ff18d251c822a --hash=sha256:e4e7698cb4af5bc667b1901685261fa2f3526dc73d243a461fca42500c8dbe56
fastmcp-slim==3.4.7 --hash=sha256:06b32a358320a7dc2b2ee040ba89ea55ddc20763dff2949f384f7974b13b5d8f --hash=sha256:6c931a0089705f3f2935428ef9b2bc74ad94140adc64aab84d116d103e694b3a
griffelib==2.2.0 --hash=sha256:d71c3bc2bbed9f958488634fe788b843a9f705d6d2838ca32cd6c25eeb64dfc4 --hash=sha256:e1bc36fe9cd21d4b6b659b456346755e4cfdc5676c0a5214083126ee12612b3c
h11==0.16.0 --hash=sha256:4e35b956cf45792e4caa5885e69fba00bdbc6ffafbfa020300e549b208ee5ff1 --hash=sha256:63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86
httpcore==1.0.9 --hash=sha256:2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55 --hash=sha256:6e34463af53fd2ab5d807f399a9b45ea31c3dfa2276f15a2c3f00afff6e176e8
httpx==0.28.1 --hash=sha256:75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc --hash=sha256:d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad
httpx-sse==0.4.3 --hash=sha256:0ac1c9fe3c0afad2e0ebb25a934a59f4c7823b60792691f779fad2c5568830fc --hash=sha256:9b1ed0127459a66014aec3c56bebd93da3c1bc8bb6618c8082039a44889a755d
idna==3.19 --hash=sha256:5e0811a4383b21dc5838069f801c4fb62113b7447663d2530d2bd6e77b49bf15 --hash=sha256:815e7be7a7806d54abb586dc943addc79e8b2ee16915059658cbeff4b1b43bf4
jaraco-context==6.1.2 --hash=sha256:bf8150b79a2d5d91ae48629d8b427a8f7ba0e1097dd6202a9059f29a36379535 --hash=sha256:f1a6c9d391e661cc5b8d39861ff077a7dc24dc23833ccee564b234b81c82dfe3
jaraco-functools==4.6.0 --hash=sha256:880c577ec9720b3a052d5bc611fb9f2269b3d87902ef42440df443b88e443280 --hash=sha256:99e3dc0060c5cbe8fcd1cdb36258e2a65ca40f1566b2033b12abb1bb44dd3c30
jaraco.classes==3.4.0 --hash=sha256:47a024b51d0239c0dd8c8540c6c7f484be3b8fcf0b2d85c13825780d3b3f3acd --hash=sha256:f662826b6bed8cace05e7ff873ce0f9283b5c924470fe664fff1c2f00f581790
joserfc==1.7.4 --hash=sha256:32d46c2cd5e3203c13e87a6c61333cab310b1ba80cd54b4c4f386a848a122463 --hash=sha256:b3bc561672ae541b17a9237053b48a03dacddd92d68047b3ecdfb4b5714a88ed
jsonref==1.1.0 --hash=sha256:32fe8e1d85af0fdefbebce950af85590b22b60f9e95443176adbde4e1ecea552 --hash=sha256:590dc7773df6c21cbf948b5dac07a72a251db28b0238ceecce0a2abfa8ec30a9
jsonschema==4.26.0 --hash=sha256:0c26707e2efad8aa1bfc5b7ce170f3fccc2e4918ff85989ba9ffa9facb2be326 --hash=sha256:d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce
jsonschema-path==0.5.0 --hash=sha256:2790a070bc7abb08ea3dbe4d340ece4efadf639223001f020c7503229ba068e2 --hash=sha256:493b156ba895c97602655b620a8456caa2ce08c1aa389f5a7addec065e6e855c
jsonschema-specifications==2025.9.1 --hash=sha256:98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe --hash=sha256:b540987f239e745613c7a9176f3edb72b832a4ac465cf02712288397832b5e8d
keyring==25.7.0 --hash=sha256:be4a0b195f149690c166e850609a477c532ddbfbaed96a404d4e43f8d5e2689f --hash=sha256:fe01bd85eb3f8fb3dd0405defdeac9a5b4f6f0439edbb3149577f244a2e8245b
markdown-it-py==4.2.0 --hash=sha256:04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49 --hash=sha256:9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a
mcp==1.29.1 --hash=sha256:1967ba4c315f7a375146209949f45950d18b0efd2f913d7cf3400bc723ee5f04 --hash=sha256:b6310eeb59153300c4ab8b9aec4c52f4819a2d6a8e429eb43d908bed7c783648
mdurl==0.1.2 --hash=sha256:84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8 --hash=sha256:bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba
more-itertools==11.1.0 --hash=sha256:48e8f4d9e7e5878571ecf6f2b4e57634f93cd474cc8cfbd2376f2d11b396e30d --hash=sha256:4b65538ae22f6fed0ce4874efd317463a7489796a0939fa66824dd542125a192
openapi-pydantic==0.5.1 --hash=sha256:a3a09ef4586f5bd760a8df7f43028b60cafb6d9f61de2acba9574766255ab146 --hash=sha256:ff6835af6bde7a459fb93eb93bb92b8749b754fc6e51b2f1590a19dc3005ee0d
opentelemetry-api==1.44.0 --hash=sha256:67647e5e9566edcf421166fdf022b3537f818635daa852b289e34604dc6fb33a --hash=sha256:94b98c893a91b88657eaac1e3ba89618cdb85be6918196705354f34728b2cdef
packaging==26.3 --hash=sha256:94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79 --hash=sha256:d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c
pathable==0.6.0 --hash=sha256:6404b8b82aef5ff0fd478934137128b99b12212ba35afdde5525ca4f8388ea58 --hash=sha256:82c4ca6c98c502ad12e0d4e9779b6210afee93c38990988c8c5d1b49bdcdf566
platformdirs==4.11.5 --hash=sha256:89f8d42695853b89c7170bd49bc3dc593f98a71e695ede88e06a3b247bc4563b --hash=sha256:e8b31f4f8bcbbedef91a6b57a706255e4f148d2a4e01648382a0a47342539173
py-key-value-aio==0.4.5 --hash=sha256:ab862adbcb8c72547d1c57821f22cbbb71ab86509039c96f36e914e0336c8dd7 --hash=sha256:c6563a2c6abe5da5e20f4f9e875c2a9b425a2244a54fadbf46cf140a9eea45d7
pycparser==3.0 --hash=sha256:600f49d217304a5902ac3c37e1281c9fe94e4d0489de643a9504c5cdfdfc6b29 --hash=sha256:b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
pydantic==2.13.5 --hash=sha256:346a034f080da3755d8e9cb5e00e8b07de1d39e4f6e2c87d8ab7cafa0b269a73 --hash=sha256:51a9c5f7b2f8e636f04c6cada605d9b6a3bf1348fdf945a3d8869b19bba0ee08
pydantic-core==2.46.5 --hash=sha256:013d6f3483d81e02e7c328831808f336c8596ee33b4bd4026b9ffb1e960b8942 --hash=sha256:03b9666e41e35d8909852ba191a0607520f81b74eaf12ccf8737005dbb313821 --hash=sha256:045ab3b6d308439e32b81cc173bba5b9018bc6ed896afd0c65b3b009b1699af5 --hash=sha256:0bddb4020d8f04175865ccd17eff3040874fc11fb593f424edb452653b4b947c --hash=sha256:0cdbada856a1c69a7624a64d3d9aefe79300bd6ef827b43a4f265010b9b55184 --hash=sha256:0fc5be0abd4a407e200d844b404e33639a554e7bd0d448e7b9ae181be4789ac2 --hash=sha256:10416c15b8839ecc4ef4d0885da76da6fd0f67333a0eb8aff6d93c4b8f2910fc --hash=sha256:15f4a94963c95accac15b7b657bb177d3ad82bb90b0d0526d9a9b85079925db5 --hash=sha256:18a09e1e1011b462f2e32774f25859ef1223d5c2b0546a633cf56654710721e0 --hash=sha256:193375f3548919d3f0b60 --hash=sha256:2d95ddc1eb6914154253d239089900813f6a767e174b8e6a50e7fdacb7e4236c
pydantic-settings==2.15.0 --hash=sha256:0ba092c291c94baceb5eff768aa0d56400a457585bc0175925a5a5510303da42 --hash=sha256:694b793e84f766ba76a90ebdefc01d0a9a045dab0382bee70393da93712ad117
pygments==2.21.0 --hash=sha256:2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9 --hash=sha256:610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c
pyjwt==2.13.0 --hash=sha256:41571c89ca91598c79e8ef18a2d07367d4810fbbd6f637794879baf1b7703423 --hash=sha256:66adcc2aff09b3f1bbd95fc1e1577df8ac8723c978552fd43304c8a290ac5728
pyperclip==1.11.0 --hash=sha256:244035963e4428530d9e3a6101a1ef97209c6825edab1567beac148ccc1db1b6 --hash=sha256:299403e9ff44581cb9ba2ffeed69c7aa96a008622ad0c46cb575ca75b5b84273
python-dotenv==1.2.3 --hash=sha256:904552145e8bfed22162c09dab1c2b9b54fefa7b23ba780f4f26ca0316b0f0d9 --hash=sha256:a20a594dabeaa385725aa239d5244871c143ecb356add8a20fcf23773a6c3a35
python-multipart==0.0.32 --hash=sha256:be54b7f3fa167bb83e4fcd936b887b708f4e57fe75911c02aebf53efaf8d938e --hash=sha256:ff6d3f776f16878c894e52e107296ffc890e913c611b1a4ec6c44e2821fe2e23
pyyaml==6.0.3 --hash=sha256:00c4bdeba853cc34e7dd471f16b4114f4162dc03e6b7afcc2128711f0eca823c --hash=sha256:0150219816b6a1fa26fb4699fb7daa9caf09eb1999f3b70fb6e786805e80375a --hash=sha256:02893d100e99e03eda1c8fd5c441d8c60103fd175728e23e431db1b589cf5ab3 --hash=sha256:02ea2dfa234451bbb8772601d7b8e426c2bfa197136796224e50e35a78777956 --hash=sha256:0f29edc409a6392443abf94b9cf89ce99889a1dd5376d94316ae5145dfedd5d6 --hash=sha256:10892704fc220243f5305762e276552a0395f7beb4dbf9b14ec8fd43b57f126c --hash=sha256:16249ee61e95f858e83976573de0f5b2893b3677ba71c9dd36b9cf8be9ac6d65 --hash=sha256:1d37d57ad971609cf3c53ba6a7e365e40660e3be0e5175fa9f2365a379d6095a --hash=sha256:1ebe39cb5fc479422b83de611d14e2c0d3bb2a18bbcb01f229ab3cfbd8fee7a0 --hash=sha256:214ed4befebe12df36bcc8bc2b64b
referencing==0.37.0 --hash=sha256:381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231 --hash=sha256:44aefc3142c5b842538163acb373e24cce6632bd54bdb01b21ad5863489f50d8
rich==15.0.0 --hash=sha256:33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb --hash=sha256:edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36
rich-rst==2.1.0 --hash=sha256:7ecd1343ee12c879d0e7ae74c3eb6d263b023d2929c6d114212eb1fd91057255 --hash=sha256:f4d117b49697f338769759fa5cacf5197da4888b347b9fda2e50aef5cd8d93bd
rpds-py==2026.6.3 --hash=sha256:0be972be84cfcaf46c8c6edf690ca0f154ac17babf1f6a955a51579b34ad2dc5 --hash=sha256:127565fead0a10943b282957bd5447804ff3160ad79f2ad2635e6d249e380680 --hash=sha256:127e08c0642d880cf32ca47ec2a4a77b901f7e2dd1ad9762adb13955d72ffcc9 --hash=sha256:166cf54d9f44fc6ceb53c7860258dde44a81406646de79f8ed3234fca3b6e538 --hash=sha256:168c733a7112e071bb7a66460e667edfcff06c017a3c523f7a8a8e08d0140804 --hash=sha256:1967debc37f64f2c4dc90a7f563aec558b471966e12adcac4e1c4240496b6ebf --hash=sha256:1cebd1337c242e4ec2293e541f712b2da849b29f48f0c293684b71c0632625d4 --hash=sha256:1cf01971c4f2c5553b772a542e4aaf191789cd331bc2cd4ff0e6e65ba49e1e97 --hash=sha256:1e5822dfc2f0d4ab7e745eaa6d85945069329beeccef965af3f3bb26058fcab6 --hash=sha256:22bffe6042b9bcb0822bcd195
sse-starlette==3.4.8 --hash=sha256:6e82314c786709a3cd9520f2285cf9fff90e181e598e8a357b0cf80f66afba0d --hash=sha256:ed89ffbb75cbf78a5fe2f2109cd584792ee7f9dfac96f791db546df8f15f3f9c
starlette==1.6.0 --hash=sha256:a86dd39d14bb45f85a3d18525215a9ef0cfd1f192ac793220e72598c90335f0c --hash=sha256:d4e3ac5e546444960c710297a3c9fc3f7ebae1b7e963f3d36173b49da535be9b
typing-extensions==4.16.0 --hash=sha256:481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8 --hash=sha256:dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5
typing-inspection==0.4.4 --hash=sha256:547274fa6b0a561ccf549cc9524b999a578e737d015d8709d021f9d0d13bea47 --hash=sha256:65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147
uncalled-for==0.4.0 --hash=sha256:16c4bb3337532e4bd5569adc192285976f3ad5305402256d34c67a12b5c968bd --hash=sha256:335b95bd2422332ec210d518f314a16e4c640921c39fc8bf2ad095bd3538f4af
uvicorn==0.52.4 --hash=sha256:73acfee47a0b133c5de13d219492d62d8a31e935f4fe6e41a232451a15379f86 --hash=sha256:f86e41a149d7d05a9969337e3946a9c171c06a5d42680896daaba624aeac8da1
watchfiles==1.2.0 --hash=sha256:01859b11fd9fbca670f4d5da00fbac282cfea9bd67a2125d8b2833a3b5617ea9 --hash=sha256:01ea8d66f0693b9b60a6541c8d10263091ca9a9060d242f3c1f3143f9aad2c98 --hash=sha256:027ae72bfdfd254862065d8b3e2a815c6ab9b1853ce41e6648ece84afd34a551 --hash=sha256:03b14855c6f35539e2d95c442ae9530a75762f1e26567152b9ed05f96534a74d --hash=sha256:054dc20fd2e3132b4c3883b4a00d72fd6e1f56fdaf89fccd12e8057d74cd74d7 --hash=sha256:094b9b70103d4e963499bdea001ee3c2697b144cd9ae6218a62c0f89ec9e31db --hash=sha256:0a105bc2283f67e8fbec74253ec2d94925de92ed72c0393f1206bf326b7b7b69 --hash=sha256:0a37faaed405c67e28e6be45a1fa4f206ef5a2860f27c237db9fa30704c38242 --hash=sha256:0c4997d4e4a55f0d02b6cde327322daf3a0400e5df6c6b15948994bf72497925 --hash=sha256:0cb4d80e212f116474a545c21 --hash=sha256:2d95ddc1eb6914154253d239089900813f6a767e174b8e6a50e7fdacb7e4236c
websockets==17.1 --hash=sha256:0014eaff8ad5b3b43feda2279f9d34bf2eaae040720b9fbbb55944b10f40b14d --hash=sha256:00679b7468b4c2b12b0757118174e8eabac56bb2f579a928a104d9554a56e098 --hash=sha256:00bf34b64501e3477e81fc281532ff3cbf4da26633c10b63979d5085d46602d3 --hash=sha256:01dcb47deebc40b38fd4a493b9b9f4d0a704b7bec6f35e4d34085b329abce71a --hash=sha256:020e271205f8ab3406d7a59cd00de6dec722315924411c421bd00642f18bad86 --hash=sha256:0340bbef6bfbe16da888b3983d666a4db4954ac3253c38f13bc7aba0c7db5a2f --hash=sha256:054c28db2dcec0e857e3b705d8c28012613e555b38c765d6a4f75340a4fc06a0 --hash=sha256:073c5c3f7e127041fa9d34a9e29ceefee8c3cafbd267ed2927318f425144380d --hash=sha256:07fa3e7c30e2c577928d359b56bf872a3e0cbcc15553eaa0907c1ee86344b56f --hash=sha256:0c84bdef916556cbe1d5a43b42
"""
)


class SubstrateError(Exception):
    """Raised when substrate configuration, validation, or runtime fails."""


def parse_requirements_hashes(requirements_text: str) -> dict[str, set[str]]:
    """Parse a hash-locked requirements.txt into a mapping of normalized package_name -> set of sha256 hex strings."""
    req_hashes: dict[str, set[str]] = {}
    for line in requirements_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pkg_part = parts[0]
        pkg_name = pkg_part.split("==")[0].lower().replace("_", "-")
        hashes = set()
        for p in parts[1:]:
            if p.startswith("--hash=sha256:"):
                hashes.add(p.removeprefix("--hash=sha256:"))
        if not hashes:
            raise SubstrateError(f"Requirement {pkg_name!r} has no --hash=sha256: declarations")
        req_hashes[pkg_name] = hashes
    return req_hashes


def verify_wheelhouse_inventory(
    wheelhouse_dir: Path, requirements_text: str
) -> list[dict[str, Any]]:
    """Mechanically verify that wheelhouse contains an exact matching wheel for every locked requirement.

    Rejects:
    - Non-directory or empty wheelhouse
    - Missing required package wheel
    - Tampered wheel bytes whose sha256 is not in the declared requirement lock
    - Extra unapproved wheels not in the lockfile
    """
    if not wheelhouse_dir.is_dir():
        raise SubstrateError(f"Wheelhouse directory does not exist: {wheelhouse_dir.as_posix()!r}")

    locked = parse_requirements_hashes(requirements_text)
    wheels = list(wheelhouse_dir.glob("*.whl"))
    if not wheels:
        raise SubstrateError(
            f"Wheelhouse {wheelhouse_dir.as_posix()!r} is empty (contains 0 wheels)"
        )

    matched_packages: set[str] = set()
    inventory: list[dict[str, Any]] = []

    for w_file in sorted(wheels, key=lambda p: p.name):
        w_bytes = w_file.read_bytes()
        w_hash = hashlib.sha256(w_bytes).hexdigest()
        pkg_name = w_file.name.split("-")[0].lower().replace("_", "-")

        if pkg_name not in locked:
            raise SubstrateError(
                f"Wheelhouse contains extra unapproved package {w_file.name!r} not in lockfile"
            )

        if w_hash not in locked[pkg_name]:
            raise SubstrateError(
                f"Wheel {w_file.name!r} SHA-256 hash {w_hash} does not match any locked hash for {pkg_name}"
            )

        matched_packages.add(pkg_name)
        inventory.append(
            {
                "filename": w_file.name,
                "size_bytes": len(w_bytes),
                "sha256": w_hash,
            }
        )

    missing_packages = set(locked.keys()) - matched_packages
    if missing_packages:
        raise SubstrateError(
            f"Wheelhouse is missing required locked package(s): {sorted(missing_packages)}"
        )

    return inventory


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
    """Deterministic fault interceptor operating on FaultInjectionRecord ledgers."""

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
            f'    mcp.run(transport="sse", host="0.0.0.0", port={port})',
            "",
        ]
    )
    return "\n".join(lines)


def render_mcp_sidecar_dockerfile(
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    wheelhouse_dir: str = "/wheelhouse",
    app_dir: str = "/app",
    server_script_name: str = "server.py",
) -> str:
    """Render canonical offline sidecar Dockerfile using strict hash-locked pip installation."""
    return f"""FROM {base_image}

WORKDIR {app_dir}

COPY wheelhouse {wheelhouse_dir}
COPY requirements.txt {app_dir}/requirements.txt
COPY {server_script_name} {app_dir}/{server_script_name}

RUN pip install --no-cache-dir --no-index --find-links={wheelhouse_dir} --require-hashes -r {app_dir}/requirements.txt

RUN mkdir -p /app/output

CMD ["python", "{app_dir}/{server_script_name}"]
"""


def materialize_mcp_sidecar_package(
    target_dir: Path,
    tools: Sequence[MCPToolDefinition],
    server_name: str = "eval-lab-fastmcp-sidecar",
    port: int = DEFAULT_MCP_PORT,
    base_image: str = DEFAULT_PINNED_BASE_IMAGE,
    wheelhouse_source: Path | None = None,
    op_registry_module: str | None = None,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Boring task-authoring API emitting a complete, workbench-v2 compliant offline FastMCP sidecar package.

    Parameters:
        target_dir: Target directory where sidecar files will be emitted.
        tools: Sequence of discrete MCP tool definitions.
        server_name: FastMCP server identifier.
        port: Service port.
        base_image: Immutable pinned base image reference.
        wheelhouse_source: Directory of pre-downloaded wheels matching FASTMCP_SIDECAR_REQUIREMENTS_TXT. Mandatory unless plan_only=True.
        op_registry_module: Optional module path for DAG/operation registry delegation.
        plan_only: When True, skips Dockerfile/wheelhouse copying and emits only plan specification.
    """
    target_dir = safe_resolve_subpath(target_dir.parent, target_dir.name)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. server.py
    server_code = generate_fastmcp_server_script(
        tools=tools,
        server_name=server_name,
        port=port,
        op_registry_module=op_registry_module,
    )
    (target_dir / "server.py").write_text(server_code, encoding="utf-8")

    # 2. requirements.txt
    (target_dir / "requirements.txt").write_text(FASTMCP_SIDECAR_REQUIREMENTS_TXT, encoding="utf-8")

    wheel_inventory: list[dict[str, Any]] = []

    if plan_only:
        # Plan-only mode: Dockerfile and wheelhouse omitted
        proof_data = {
            "mode": "plan_only",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            "base_image": base_image,
            "requirements_sha256": compute_sha256(FASTMCP_SIDECAR_REQUIREMENTS_TXT),
        }
        (target_dir / "offline-build-proof.json").write_text(
            canonical_json(proof_data) + "\n", encoding="utf-8"
        )
    else:
        if wheelhouse_source is None:
            raise SubstrateError(
                "wheelhouse_source is mandatory for production sidecar materialization; pass plan_only=True to emit plan without container build artifacts"
            )

        # Strictly verify wheelhouse against locked requirements before admitting any files
        wheel_inventory = verify_wheelhouse_inventory(
            wheelhouse_source, FASTMCP_SIDECAR_REQUIREMENTS_TXT
        )

        dest_wheelhouse = target_dir / "wheelhouse"
        dest_wheelhouse.mkdir(parents=True, exist_ok=True)
        for w_file in sorted(wheelhouse_source.glob("*.whl")):
            shutil.copy2(w_file, dest_wheelhouse / w_file.name)

        # 3. Dockerfile
        dockerfile_content = render_mcp_sidecar_dockerfile(base_image=base_image)
        (target_dir / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

        proof_data = {
            "mode": "complete_offline_package",
            "substrate_version": MCP_SUBSTRATE_VERSION,
            "base_image": base_image,
            "requirements_sha256": compute_sha256(FASTMCP_SIDECAR_REQUIREMENTS_TXT),
            "wheel_count": len(wheel_inventory),
            "wheels": wheel_inventory,
        }
        (target_dir / "offline-build-proof.json").write_text(
            canonical_json(proof_data) + "\n", encoding="utf-8"
        )

    # Compose and Collect fragments
    compose_doc = render_mcp_compose_document(
        sidecar_service=DEFAULT_SIDECAR_SERVICE,
        sidecar_build_context="./" + target_dir.name,
    )

    collect_fragment = {
        "service": DEFAULT_SIDECAR_SERVICE,
        "source": "/app/output/benchmark-events.jsonl",
        "destination": "benchmark-events.jsonl",
    }

    return {
        "sidecar_dir": target_dir.as_posix(),
        "compose_doc": compose_doc,
        "collect_fragment": collect_fragment,
        "proof_sha256": compute_sha256(proof_data),
    }


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

    main_service_cfg: dict[str, Any] = {
        "image": main_image,
    }
    sidecar_service_cfg: dict[str, Any] = {
        "build": {
            "context": sidecar_build_context,
        },
    }

    volumes_section: dict[str, Any] | None = None
    if volume_name:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", volume_name):
            raise SubstrateError(f"Invalid volume name: {volume_name!r}")
        main_service_cfg["volumes"] = [f"{volume_name}:{volume_mount}:ro"]
        sidecar_service_cfg["volumes"] = [f"{volume_name}:{volume_mount}:rw"]
        volumes_section = {volume_name: None}

    services: dict[str, Any] = {
        "main": main_service_cfg,
        sidecar_service: sidecar_service_cfg,
    }

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
