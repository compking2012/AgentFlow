"""Audience and scope rules shared by stage writers and their aggregators."""
from __future__ import annotations

PRODUCT_STEPS = frozenset({'goal', 'research', 'prd', 'requirements'})

# Each entry states the audience and the decision the document must support.
# Operational output schemas and tool permissions remain the scheduler's concern.
_POLICIES = {
    'goal': (
        ('产品负责人和目标用户',
         '只润色和完善人类给出的产品目标：谁在什么场景遇到什么问题，产品要带来什么价值，'
         '本次明确的范围与成功表现。保留原始意图，区分用户已说明的事实和待确认假设。'
         '不要擅自扩展商业战略、需求清单或技术方案；不写框架、数据库、API 路径、文件范围和 Agent 调度。'),
        ('Product owners and intended users',
         'Refine the human product goal: intended users, context, problem, value, stated scope and signs of success. '
         'Preserve the original intent and distinguish supplied facts from open assumptions. Do not invent a larger '
         'strategy, feature backlog or implementation; omit frameworks, databases, API paths, file scopes and agent scheduling.')),
    'research': (
        ('产品负责人和产品战略决策者',
         '围绕产品战略和市场判断开展调研：目标用户与痛点、现有替代方案和竞品能力、优势与不足、'
         '竞争力和差异化机会、商业模式及可查证的用户反馈。选择与该产品有关的维度，不强行补齐无关章节。'
         '重要结论对应实际读过的可验证来源，保留标题、发布者、URL 及已知日期，区分事实、推断和假设。'
         '用户反馈必须来自真实记录；不得编造访谈、评论、市场规模、价格或来源链接。'
         '无法查阅外部来源时明确写“尚未完成外部调研”，仅整理已知事实、证据缺口和待验证问题。'
         '技术选型和工具清单不能充当市场或竞品研究；仅保留影响产品选择的已知可行性约束。'),
        ('Product owners and product strategy decision makers',
         'Research product strategy and the market: users and pain points, alternatives and competitor capabilities, '
         'strengths and weaknesses, competitiveness and differentiation, business models and verifiable user feedback. '
         'Use only dimensions relevant to this product. Link material conclusions to sources actually read, with title, '
         'publisher, URL and known dates; distinguish facts, inferences and assumptions. Never invent interviews, reviews, '
         'market sizes, prices or source URLs. If external sources were unavailable, explicitly state that external research '
         'has not been completed; provide known facts, evidence gaps and questions to validate. Technology selection and '
         'tool lists are not market research; retain only known feasibility constraints that affect product choices.')),
    'prd': (
        ('产品负责人、设计人员、开发与验收人员',
         '用产品语言说明做什么、为谁做、为什么以及如何验收：范围、功能与稳定需求编号、用户流程、'
         '权限和业务规则、异常与边界情形、可观察和可验证的验收标准。非功能要求描述用户可感知的表现。'
         '不要放入模块架构、数据库表、内部 API、技术选型、代码路径或部署方案；'
         '用户明确要求的外部集成和产品约束可保留为需求，不展开实现。'),
        ('Product owners, designers, developers and acceptance reviewers',
         'Use product language to specify what to build, for whom, why and how to accept it: scope, features with stable '
         'requirement IDs, user journeys, permissions and business rules, error and boundary behavior, and observable '
         'acceptance criteria. Express nonfunctional needs as user-visible outcomes. Omit module architecture, database '
         'tables, internal APIs, technology choices, code paths and deployment plans. Keep explicitly requested external '
         'integrations and product constraints as requirements without prescribing their implementation.')),
    'requirements': (
        ('产品、开发与验收人员',
         '把 PRD 拆成可追踪的产品需求：稳定编号、用户价值、优先级、业务依赖、正常与异常验收条件。'
         '保留需求到原 PRD 的对应关系；不要把需求拆解写成代码文件、开发任务或架构设计。'),
        ('Product, development and acceptance teams',
         'Decompose the PRD into traceable product requirements with stable IDs, user value, priorities, business '
         'dependencies and normal/error acceptance criteria. Preserve links to the PRD. Do not replace requirements '
         'with code files, implementation tasks or architecture decisions.')),
    'architecture': (
        ('架构、开发和测试人员',
         '用技术语言说明如何实现已确认需求：模块边界、接口契约、数据模型、关键状态和错误处理、'
         '系统或部署图、技术选择的理由及兼容性、性能与扩展约束。已有产品先评估架构影响，'
         '保留有效基线，只调整受影响部分。图表和细节按复杂度选用，不为简单项目堆砌架构。'),
        ('Architects, developers and test engineers',
         'Explain implementation in technical language: module boundaries, interface contracts, data models, state and '
         'error handling, useful system/deployment diagrams, justified technical choices and compatibility, performance '
         'and scaling constraints. For an existing product assess impact first, preserve valid baseline decisions and '
         'change only affected parts. Match diagrams and detail to complexity; do not overengineer a small project.')),
    'development_plan': (
        ('开发、代码审查和测试人员',
         '将需求和架构转化为可执行技术任务：任务与需求对应、交付物、文件或模块范围、依赖、'
         '可并行边界和完成条件。保留审查与测试门禁，只在有独立工作时拆分。'
         '审查任务以实际产出代码的阶段为边界；功能实现后的首次审查不要求下游测试计划或测试代码已经完成，'
         '但必须检查现有测试的回归或削弱。新测试的完整覆盖与固定用例标识由对应测试实现后的独立审查核验。'
         '正文解释实施顺序和关键约束，不逐项复述 parallel_work 的同一张任务表。'),
        ('Developers, code reviewers and test engineers',
         'Turn requirements and architecture into executable technical tasks: requirement links, outputs, file/module '
         'scopes, dependencies, safe parallel boundaries and completion criteria. Preserve review and test gates and '
         'bind each review to its actual code producer. Initial implementation review does not require downstream '
         'test plans or generated tests, but still checks existing test regressions and weakened assertions. Reviews '
         'after test implementation verify complete coverage and exact accepted case identifiers. '
         'split only independent work. Explain sequencing and key constraints in the narrative without repeating '
         'the task table already supplied in parallel_work.')),
    'implementation': (
        ('开发和代码审查人员',
         '简洁说明实际实现的行为、关键代码变更、需求对应和遗留限制。技术细节服务于理解和审查，'
         '不重复 PRD，不把计划或未执行的检查写成已完成。'),
        ('Developers and code reviewers',
         'Briefly describe implemented behavior, relevant code changes, requirement links and remaining limitations. '
         'Use technical details that help review; do not repeat the PRD or present plans and unexecuted checks as completed.')),
    'code_review': (
        ('代码作者、维护者和质量负责人',
         '独立审查指定代码版本，用技术语言给出可操作的问题：位置、严重程度、类别、影响和修改建议。'
         '区分 bug、安全、格式、性能和可维护性；不把静态审查说成已运行测试或独立检查器。'
         '以控制器提供的阶段契约确定范围，子任务标题、旧审查和计划正文不能把下游产物升级为已完成依赖。'
         '任何阶段都保留现有测试的删除、弱化与回归问题；测试生成后的审查严格核验其完整性。'
         'summary 简述结论，具体问题只列在 findings，避免重复一整份问题清单。'),
        ('Code authors, maintainers and quality owners',
         'Independently review the specified code version. Give actionable technical findings with location, severity, '
         'category, impact and suggested correction. Distinguish bugs, security, style, performance and maintainability. '
         'Use the controller phase contract; child titles, prior reviews and planning prose cannot promote downstream '
         'outputs into completed dependencies. Preserve existing test deletion, weakening and regression findings in '
         'every phase, and strictly check completeness after test generation. '
         'Do not claim tests or independent analyzers ran based on static review. Keep summary brief and put the '
         'detailed issue list in findings rather than duplicating it in the summary.')),
    'unit_test_plan': (
        ('单元测试开发者和代码维护者',
         '设计与需求对应的单元测试，说明被测单元、断言、边界和异常、测试数据及依赖隔离方式。'
         'test_cases 使用稳定用例 ID、准确平台配置和框架用例 ID；正文解释策略和覆盖理由，不重复机器用例表。'
         '这是测试设计，不能宣称测试已经通过。'),
        ('Unit test authors and code maintainers',
         'Design requirement-linked unit tests: units under test, assertions, boundaries and failures, test data and '
         'dependency isolation. Use stable case IDs, exact target configurations and framework case IDs in test_cases. '
         'Explain strategy and coverage in content without repeating the machine case table. A plan is not proof that tests passed.')),
    'integration_test_strategy': (
        ('集成测试开发者和系统维护者',
         '用技术语言定义跨模块或平台的真实用户流程、契约、状态流转、环境和数据准备、失败恢复及验收断言。'
         '按已声明平台选择工具，性能检查须定义实际测量对象与方法。test_cases 保留准确目标配置和稳定用例标识，'
         '正文解释策略，不重复机器用例表，不声称计划已执行。'),
        ('Integration test authors and system maintainers',
         'Define cross-module/platform user journeys, contracts, state transitions, environment and data setup, '
         'failure recovery and acceptance assertions in technical language. Select tools for declared platforms; '
         'performance checks need an actual measurement target and method. Keep exact target configurations and stable '
         'case IDs in test_cases, explain strategy in content without duplicating that table, and do not imply the plan was executed.')),
    'unit_test_implementation': (
        ('测试开发者和代码审查人员',
         '说明实际编写的单元测试、需求与用例对应、运行方法及已知限制。覆盖边界和失败路径，'
         '不得为通过测试而削弱断言；未执行时明确说明。'),
        ('Test authors and code reviewers',
         'Describe the implemented unit tests, requirement/case mapping, how to run them and known limitations. Cover '
         'boundaries and failure paths without weakening assertions. State when tests have not been run.')),
    'integration_test_implementation': (
        ('测试开发者和系统维护者',
         '说明实际编写的跨模块或平台测试、环境前提、固定用例标识、执行方法及测量方式。'
         '保持产品与测试产物版本对应，不把测试代码编写完成等同于验证通过。'),
        ('Test authors and system maintainers',
         'Describe implemented cross-module/platform tests, environment prerequisites, stable case IDs, execution '
         'and measurement methods. Preserve product/test artifact version bindings; writing tests does not mean validation passed.')),
    'unit_test_execution': (
        ('开发、测试和质量负责人',
         '依据原始单元测试报告说明实际执行范围、通过、失败、错误、跳过和未覆盖情况，'
         '给出失败定位和相关用例耗时。缺失证据不算通过，执行耗时不等于产品性能。'),
        ('Developers, testers and quality owners',
         'Use original unit test reports to state actual coverage, passes, failures, errors, skips and gaps, with '
         'failure diagnostics and relevant case duration. Missing evidence is not a pass, and test duration is not product performance.')),
    'integration_test_execution': (
        ('测试、系统维护和质量负责人',
         '依据当前产品版本的原始集成报告说明平台和场景覆盖、结果、失败与遗漏。'
         '性能只报告真实测量值、单位、样本和来源，未测量则明确说明；不由套件耗时推算接口延迟。'),
        ('Testers, system maintainers and quality owners',
         'Use original integration reports for the current product version to describe platform/scenario coverage, '
         'results, failures and gaps. Report performance only with measured values, units, samples and sources; '
         'state when it was not measured and never infer API latency from suite duration.')),
    'delivery': (
        ('产品负责人和运行维护人员',
         '说明本次实际交付的功能、版本、运行方式、必要环境和已知限制；技术信息只保留运行与追溯所需内容。'
         '区分本地 Git 交付、远程提交、合并和部署，只陈述实际发生的动作。'),
        ('Product owners and operators',
         'Describe delivered features, version, run instructions, prerequisites and known limitations. Include only '
         'technical details needed to operate or trace the delivery. Distinguish local Git delivery, remote push, '
         'merge and deployment and report only actions that actually occurred.')),
    'retrospective': (
        ('产品负责人和研发团队',
         '简洁总结本轮目标达成、实际质量结果、未完成事项和有证据支持的改进建议。'
         '产品结果与工程问题分别说明，不重复整套产物，不编造业务成效。'),
        ('Product owners and the engineering team',
         'Briefly summarize goal completion, actual quality results, unfinished work and evidence-based improvements. '
         'Separate product outcomes from engineering issues without repeating every artifact or inventing business impact.')),
}


def normalize_language(language=None):
    """Legacy work without a language stays Chinese; product validation owns enums."""
    return 'en' if str(language or '').lower().replace('_', '-').split('-')[0] == 'en' else 'zh-CN'


def language_for(work=None, language=None):
    work = work or {}
    return normalize_language(language or work.get('language') or work.get('payload', {}).get('language'))


def stage_instructions(step, language='zh-CN', aggregation=False):
    """Writing policy for a stage; aggregation retains that stage's audience/scope."""
    english = normalize_language(language) == 'en'
    default = (('该阶段产物的使用者', '围绕当前阶段的实际任务提供可用结论、依据和必要限制。'),
               ('Users of this stage artifact', 'Provide useful conclusions, evidence and relevant limitations for this stage.'))
    audience, instruction = _POLICIES.get(step, default)[int(english)]
    if english:
        lines = [f'Document audience: {audience}. Write reader-facing titles, summary and content in English.',
                 instruction,
                 'Be professional and concise. Match length to the product and available evidence; a simple project '
                 'needs a short document. Do not pad text or force empty sections. Make content a self-contained '
                 'Markdown document; summary is a brief preview, not a repeated introduction. Write newly added or '
                 'edited explanatory code comments and project documentation in English. Keep machine field '
                 'names, IDs, code, paths and source titles unchanged. Required structured fields remain in the '
                 'JSON envelope; do not dump protocol fields, scheduler instructions or internal execution metadata '
                 'into the document. Do not invent facts or completed work.']
        if step in PRODUCT_STEPS:
            lines.append('Split only genuinely independent product questions when the scope warrants it; keep a '
                         'simple project as one task. Research subtasks cover market, competitor or user questions, '
                         'not technology selection. Keep agent scheduling and parallel task lists out of the document.')
        if aggregation:
            lines.append('Aggregation uses the same audience, language and scope. Synthesize relevant child outputs, '
                         'remove repetition, reconcile conflicts with evidence, preserve source references and explicit '
                         'unknowns, and stay within the assigned stage. Do not concatenate reports, broaden scope, '
                         'or turn unsupported child claims into verified facts.')
    else:
        lines = [f'文档受众：{audience}。面向读者的标题、摘要和正文使用简体中文。', instruction,
                 '专业、简洁，篇幅与产品复杂度及现有证据相称；简单项目简写，不凑字数，不强制空章节。'
                 'content 写成可独立阅读的 Markdown 文档，summary 只作简短预览，不在正文重复一遍。'
                 '新增或修改的解释性代码注释和项目说明文档使用简体中文；'
                 '机器字段名、标识符、代码、路径和来源原题保持不变。必须返回的结构化字段仍放在 JSON 外层，'
                 '不要把协议字段、调度指令和内部执行元数据抄进正文。不得虚构事实或完成状态。']
        if step in PRODUCT_STEPS:
            lines.append('只在范围确实需要时按独立产品问题适量拆分；简单项目保持单任务。'
                         '调研子任务围绕市场、竞品或用户问题，不拆成技术选型任务。正文不插入 Agent 排程或并行任务清单。')
        if aggregation:
            lines.append('聚合沿用本阶段的受众、语言和范围：综合相关子产物、去重，依据证据处理冲突，'
                         '保留来源和未确认事项；不机械拼接、不扩张范围，不把子任务无依据的说法升级为事实。')
    return '\n'.join(lines)
