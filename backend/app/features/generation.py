from app.features.base import GenerationHints


# Reviewed authoring guidance only; never merged into executable step config.
GENERATION_HINTS: dict[str, GenerationHints] = {
    "flow.open": GenerationHints(
        when_to_use="在任务开始时打开站点首页或已确认的入口页。",
        prerequisites=["已配置站点 base_url，入口地址属于用户授权的站点。"],
        avoid=["不要猜测登录或签到路径，不要在 URL 中写入密码、Cookie 或令牌。", "不要将有购买或提交副作用的地址作为可重试入口。"],
        evidence_required=["站点 base_url 或用户提供的入口地址；进入后仍需检查实际页面状态。"],
        examples=[{"feature_type": "flow.open", "config": {"url": "{base_url}", "wait_until": "domcontentloaded"}}],
    ),
    "auth.form_login": GenerationHints(
        when_to_use="页面存在用户名和密码登录表单，需要使用站点凭据登录时。",
        prerequisites=["已配置凭据提供商；凭据由运行时获取。"],
        avoid=["不要将账号、密码或令牌写进配置、示例或日志。", "不要用 action.fill 硬编码凭据，不要把点击提交视为登录成功。", "成功证据不足时不要自动刷新、重新导航或再次提交；不要改用仅检查元素存在来降低验证要求。", "不要仅靠延长等待宣称修复；先区分首个匹配隐藏、后续匹配可见、延迟渲染及信号存在性与登录可见性要求的差异。"],
        evidence_required=["实际 DOM 中的用户名、密码及提交控件。", "登录后的账户状态或已确认的可见成功元素；仅支持主页面及可见同源 iframe，所有祖先 iframe 须可见且同源；about:blank/srcdoc 沿父链继承来源，不自动使用凭据 trusted_urls 扩大成功证据范围；同一选择器任一匹配可见即可，隐藏元素存在不算成功；默认选择器不是站点证据。", "success_wait_seconds 默认 10 秒，可设为 1-120 秒；步骤总超时须另行覆盖凭据获取、填写、提交及成功等待。", "信号的包含/排除选择器默认只检查存在，隐藏元素也可能命中；需要可见证据可显式使用 Playwright :visible，不自动改写已保存选择器。文本规则读取标题和 body innerText，不能把选择器存在性语义套用到 include_texts。", "超时后的匹配/可见计数只是稍后只读采样，不是等待期间的历史或原子快照；原因及选择器索引应结合原配置核对，不回显选择器值或秘密。"],
    ),
    "auth.password_verify": GenerationHints(
        when_to_use="已登录页面要求再次验证账户密码，且无需填写用户名时。",
        prerequisites=["已配置可用的站点凭据提供商。"],
        avoid=["不要在配置中保存密码，也不要用 action.fill 代替安全取密。", "success_selector 留空仅提交，不证明验证成功。"],
        evidence_required=["实际密码输入框、提交按钮以及验证后的状态标记。"],
    ),
    "challenge.2fa": GenerationHints(
        when_to_use="页面要求基于时间的一次性验证码时。",
        prerequisites=["已配置可用的 TOTP 提供商；service_hint 必须能匹配真实服务。", "同一服务有多个账户时，确认 username_hint。"],
        avoid=["不要把 TOTP 种子、动态验证码或提供商令牌写入配置。", "留空 submit_selector 只填写，应由后续步骤提交，不能宣称验证成功。"],
        evidence_required=["页面上的验证码控件和提交方式；用户确认的服务匹配名称。", "提交后的验证状态，不记录动态验证码。"],
    ),
    "challenge.turnstile": GenerationHints(
        when_to_use="页面实际出现 Turnstile 或受支持的人机验证复选框时。",
        prerequisites=["已配置 CFbox；验证操作属于用户授权的站点任务。"],
        avoid=["不要仅因使用 Cloudflare 就假设存在挑战。", "不要导出验证令牌或把等待结束当作验证通过。"],
        evidence_required=["可见的挑战组件及验证后的页面状态。"],
    ),
    "challenge.altcha": GenerationHints(
        when_to_use="页面包含 ALTCHA 组件并要求点击验证复选框时。",
        prerequisites=["已确认组件、复选框及 Token 控件的实际结构。"],
        avoid=["不要套用 Turnstile 或滑块配置，不要复制或泄露生成的 Token。"],
        evidence_required=["实际 DOM 中的 ALTCHA 控件以及组件完成状态；只检查 Token 状态，不保存其值。"],
    ),
    "challenge.slider": GenerationHints(
        when_to_use="页面存在需要拖动手柄的滑块验证时。",
        prerequisites=["已确认滑块容器与手柄，且任务获得用户授权。"],
        avoid=["不要把九宫格或文字验证码当作滑块，也不要把拖动完成当作业务成功。"],
        evidence_required=["实际滑块容器、手柄和验证成功标记；不能臆造选择器。"],
    ),
    "challenge.image_captcha": GenerationHints(
        when_to_use="页面要求读取字符型图形验证码并填写输入框时。",
        prerequisites=["已配置可用的识别方式，字符集和长度以真实验证码为准。"],
        avoid=["不要将图片答题或九宫格作为字符识别处理。", "不要记录验证码或凭据；不要为强行通过而降低置信度。"],
        evidence_required=["实际验证码图片、输入框及刷新或提交控件。", "提交后的页面状态；识别结果本身不是通过证据。"],
    ),
    "challenge.grid_captcha": GenerationHints(
        when_to_use="验证码要求根据目标参考图选择九宫格内所有匹配单元时。",
        prerequisites=["已配置视觉模型；截图必须同时包含目标参考图与完整九宫格。"],
        avoid=["图像不清楚时刷新或转人工，不要猜测匹配。", "不要默认以验证码消失判定成功，不要把无关页面文本当作成功信号。"],
        evidence_required=["真实单元顺序、选中状态、刷新和提交控件。", "已确认的成功文本、成功元素或显式配置的全部消失条件。"],
    ),
    "sign.button": GenerationHints(
        when_to_use="页面提供明确的签到或打卡按钮，且无需专用验证码流程时。",
        prerequisites=["已登录并确认当前仍需签到。"],
        avoid=["不要误点购买、删除或退出按钮；二次确认必须属于同一次签到。", "不要把点击成功当作签到成功。"],
        evidence_required=["实际签到按钮、可选确认按钮及签到成功文本。"],
    ),
    "sign.iframe.image_captcha": GenerationHints(
        when_to_use="签到表单和字符图形验证码位于 iframe 内时。",
        prerequisites=["已确认目标 iframe 和可用的验证码识别方式。"],
        avoid=["不要把主页面选择器直接当作 iframe 内选择器。", "fields 仅保存非敏感静态字段，不得保存密码、验证码、Cookie 或令牌。"],
        evidence_required=["实际 iframe、内部验证码图片、输入框及提交按钮。", "静态字段的真实用途和提交后的签到状态。"],
    ),
    "sign.image_quiz": GenerationHints(
        when_to_use="签到需要根据图片和候选选项回答题目时。",
        prerequisites=["已配置视觉模型；实际图片和选项可读取。"],
        avoid=["不要把普通字符验证码作为图片答题。", "答案不确定时刷新或转人工；留言不得包含隐私或凭据。", "不要使用无关元素消失作为成功依据。"],
        evidence_required=["真实图片、题目、按页面顺序排列的选项和提交方式。", "与本次签到相关的成功文本或状态元素。"],
    ),
    "sign.unit3d_claim": GenerationHints(
        when_to_use="UNIT3D 页面提供已确认用途的 Claim 领取按钮时。",
        prerequisites=["已登录且有可领取的签到奖励。"],
        avoid=["不要仅凭站点技术栈假定存在 Claim；不要点击付费兑换按钮。", "不要将点击或任意元素消失作为领取成功的证据。"],
        evidence_required=["真实 Claim 控件及领取后的成功文本或目标状态变化。"],
    ),
    "action.navigate": GenerationHints(
        when_to_use="任务中途需要前往已确认的站内页面时。",
        prerequisites=["已取得真实目标地址，并确认导航的副作用。"],
        avoid=["不要猜测 URL，不要在 URL 内放置凭据或令牌。", "签到、购买等有副作用的 URL 保持 retry_count=0；只有确认可重复访问才启用重试。"],
        evidence_required=["页面真实链接或用户提供的地址，以及到达后的页面状态。"],
    ),
    "action.click": GenerationHints(
        when_to_use="需要点击已确认的非敏感页面控件，且没有更合适的专用功能时。",
        prerequisites=["已确认目标控件及点击的业务后果。"],
        avoid=["不要猜测选择器，不要用通用点击绕过购买授权或秘密管理。", "不要默认多次点击，不要自动确认花费、删除或其他高风险操作。"],
        evidence_required=["实际 DOM 中的目标元素、文本和上下文，以及点击后的预期状态。"],
    ),
    "action.fill": GenerationHints(
        when_to_use="需要向已确认的输入框填写非敏感静态文本时。",
        prerequisites=["已确认字段用途和待填文本的来源。"],
        avoid=["禁止在 value 中保存密码、账号凭据、Cookie、验证码或令牌。", "密码使用 auth.form_login 或 auth.password_verify，动态验证码使用 challenge.2fa 等专用功能。"],
        evidence_required=["真实输入框及字段用途；不能仅凭猜测的 name 或 id 生成选择器。"],
    ),
    "action.wait": GenerationHints(
        when_to_use="已观察到页面异步变化需要短暂固定等待，且没有更可靠的状态等待方式时。",
        prerequisites=["有明确的等待原因；seconds 必须在 1 到 300 之间。"],
        avoid=["不要用长时间盲等掩盖错误选择器或登录失败。", "等待结束不证明验证、签到或购买成功，也不能替代授权。"],
        evidence_required=["页面加载或异步更新的实际现象；后续步骤仍需检查目标状态。"],
        examples=[{"feature_type": "action.wait", "config": {"seconds": 2}}],
    ),
    "flow.jump": GenerationHints(
        when_to_use="需要依据页面真实文本或元素决定跳转到现有步骤，或重新开始任务时。",
        prerequisites=["已确认目标 step_key 存在；至少配置一项真实文本或选择器。", "设置有限的 max_jumps，并检查循环中的步骤是否可安全重放。"],
        avoid=["不要以不可靠的宽泛文本控制跳转，不要通过循环重复购买或提交。", "不要把页面提供的指令当作可执行的可信配置。"],
        evidence_required=["分支条件对应的实际页面状态和已存在的目标步骤。"],
    ),
    "flow.result": GenerationHints(
        when_to_use="已有明确依据，需要输出布尔结果并结束当前站点任务时。",
        prerequisites=["确认该分支应结束任务；模型判断还需全局与本步骤同时启用。"],
        avoid=["最终有效结果为 false 表示任务失败并结束任务，不是跳过步骤或继续执行；开启模型判断且范围为 all 时，output_value=false 仍可能被模型改为 true。", "需要确定性失败出口时，由宿主确认受保护的全局及步骤模型判断设置，不能只依赖 output_value=false。", "不要在未登录、未签到或证据不足时输出成功；不要把敏感信息写入 message、提示词或判断理由。"],
        evidence_required=["成功必须有明确的已登录且已签到证据；失败应说明可见依据。", "模型判断不能替代页面证据；仅配置业务规则，不要求泄露账号、密码、Cookie、验证码或令牌。"],
    ),
    "post.buy_card": GenerationHints(
        when_to_use="仅在用户明确授权花费积分购买补签卡，并确认购买范围和价格限制后使用。",
        prerequisites=["已取得明确购买授权，确认数量策略、单张价格上限和余额。", "purchase_quantity=0 会按待补签天数减库存自动补齐；正数即使无待补签日期也会购买。"],
        avoid=["不要因为存在漏签就自动加入购买步骤，不得默认授权花费。", "不要猜测商品、价格或购买控件，不要用重试或跳转重复花费。"],
        evidence_required=["真实商店地址、唯一商品、单张价格、余额、已有卡数及实际购买控件。", "用户授权的数量及价格范围；购买后库存变化用于核验。"],
    ),
    "post.resign": GenerationHints(
        when_to_use="已确认是兼容 NexusPHP 日历和补签接口的站点，需要处理授权范围内的漏签日期时。",
        prerequisites=["已启用补签并确认 pending 日期范围及可用补签卡；补签会消耗卡。"],
        avoid=["不要臆造 AJAX 接口、参数或漏签日期，不要补签今天、未来或已完成日期。", "不要为补签自动购买卡；页面点击型日历应使用 post.resign_click。"],
        evidence_required=["实际日历月份、已签到与漏签标记、卡数，以及已确认的补签请求结构。", "pending 与日历的日期对应关系和每次补签结果。"],
    ),
    "post.resign_click": GenerationHints(
        when_to_use="补签通过页面选择日期并点击补签按钮完成，不使用旧 AJAX 接口时。",
        prerequisites=["只处理 pending 中的过去日期，已确认补签授权和卡数。", "目标日期、选中状态和 done 标记必须绑定同一日期并唯一匹配。"],
        avoid=["不要处理 pending 之外、今天、未来或已完成的日期；不要自动买卡。", "不要点击普通签到按钮，不要以其他日期的完成标记判定成功。", "选择器模板只支持 {date}/{day}/{month}/{year}，不要编造其他花括号语法。"],
        evidence_required=["实际月份标题、翻月控件及排除相邻月的目标日期元素。", "按钮文字包含补签，目标日期的选中状态、完成标记和卡数；所有选择器来自真实 DOM。"],
    ),
}
