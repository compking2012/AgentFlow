"""Bounded static project inspection. It never runs project code or tools."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from agentflow.common import DomainError, canonical_digest
from agentflow.control.product_models import TARGET_ORDER

EXCLUDED = {'.git', '.venv', 'node_modules', 'build', 'dist', '.cache', '.codex', '.openhands',
            '__pycache__', 'Pods', '.gradle', 'vendor', 'target'}
MAX_FILES = 500
MAX_DEPTH = 4
MAX_BYTES = 1024 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024
FOUNDATION_FILES = ('src/server.mjs', 'src/http.mjs', 'src/storage.mjs', 'public/index.html',
    'tooling/build.mjs', 'tests/unit.test.mjs', 'tests/api.spec.mjs', 'tests/web.spec.mjs',
    'tests/playwright.api.config.mjs', 'tests/playwright.web.config.mjs',
    'tests/support/config.mjs', 'tests/support/fixtures.mjs', 'tests/support/global-setup.mjs')


def diagnose_project(project_path: str | Path) -> dict:
    supplied = Path(project_path).expanduser()
    if not supplied.is_absolute() or supplied.is_symlink() or not supplied.is_dir():
        raise DomainError('invalid_project_path', '请选择存在的项目绝对路径，不能使用符号链接目录', 422)
    root = supplied.resolve()
    targets, frameworks, markers, signatures = set(), set(), [], {}
    reasons, files, truncated = [], {}, False
    cache, read_bytes = {}, 0

    def marker(relative, target, reason):
        targets.add(target)
        item = {'path': relative, 'target': target, 'reason': reason}
        if item not in markers:
            markers.append(item)

    def read(relative):
        nonlocal read_bytes, truncated
        if relative in cache:
            return cache[relative]
        path = files.get(relative)
        if path is None:
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or read_bytes + info.st_size > MAX_READ_BYTES:
                    truncated = True
                    cache[relative] = None
                    return None
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None
            signatures[relative] = 'sha256:' + hashlib.sha256(data).hexdigest()
            read_bytes += len(data)
            cache[relative] = data.decode('utf-8')
            return cache[relative]
        except (OSError, UnicodeError):
            return None

    for directory, children, names in os.walk(root, followlinks=False):
        base = Path(directory)
        children[:] = sorted(name for name in children if name not in EXCLUDED and not name.startswith('.')
                             and not (base / name).is_symlink())
        depth = len(base.relative_to(root).parts)
        if depth >= MAX_DEPTH:
            truncated |= bool(children)
            children[:] = []
        for name in sorted(names):
            if name.startswith('.'):
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                break
            files[path.relative_to(root).as_posix()] = path
        if len(files) >= MAX_FILES:
            break

    package, lock = {}, {}
    for relative in files:
        name = Path(relative).name
        if name == 'package.json':
            try:
                value = json.loads(read(relative) or '')
                if not isinstance(value, dict):
                    continue
                dependencies = {**value.get('dependencies', {}), **value.get('devDependencies', {})}
                if relative == 'package.json':
                    package = value
                mobile_react = 'react-native' in dependencies
                if mobile_react:
                    frameworks.add('React Native')
                    for target in ('ios', 'android'):
                        marker(relative, target, 'React Native 移动框架标志，未验证目标构建')
                for dependency, framework in [('react', 'React'), ('vue', 'Vue'), ('next', 'Next.js'),
                    ('vite', 'Vite'), ('express', 'Express'), ('fastify', 'Fastify'), ('electron', 'Electron')]:
                    if dependency in dependencies:
                        if dependency == 'react' and mobile_react:
                            continue
                        frameworks.add(framework)
                        if dependency in {'express', 'fastify'}:
                            marker(relative, 'api', f'{framework} 依赖标志')
                        elif dependency == 'electron':
                            for target in ('macos', 'windows', 'linux'):
                                marker(relative, target, 'Electron 桌面项目标志，未验证目标构建')
                        else:
                            marker(relative, 'web', f'{framework} 依赖标志')
                if relative == 'package.json' and (root / 'src/server.mjs').is_file():
                    marker(relative, 'api', 'Node 服务入口标志')
                    frameworks.add('Node.js')
            except (ValueError, TypeError):
                reasons.append({'code': 'invalid_package_manifest', 'message': f'无法静态读取 {relative}'})
        elif name == 'index.html':
            marker(relative, 'web', 'HTML 页面入口标志')
        elif name in {'requirements.txt', 'pyproject.toml'}:
            text = (read(relative) or '').lower()
            for framework in ('fastapi', 'flask', 'django'):
                if framework in text:
                    frameworks.add(framework)
                    marker(relative, 'api', 'Python Web/API 框架标志')
        elif name in {'build.gradle', 'build.gradle.kts', 'AndroidManifest.xml'}:
            text = (read(relative) or '').lower()
            frameworks.add('Gradle' if name != 'AndroidManifest.xml' else 'Android')
            if name == 'AndroidManifest.xml' or 'com.android.' in text or 'android' in Path(relative).parts:
                frameworks.add('Android')
                marker(relative, 'android', 'Android 工程标志')
            elif 'org.springframework.boot' in text:
                frameworks.add('Spring Boot')
                marker(relative, 'api', 'Spring Boot 服务框架标志')
        elif name == 'project.pbxproj':
            text = (read(relative) or '').lower()
            frameworks.add('Xcode')
            apple = [target for text_marker, target in [('iphoneos', 'ios'), ('macosx', 'macos')] if text_marker in text]
            for target in apple or [t for t in ('ios', 'macos') if t in Path(relative.lower()).parts]:
                marker(relative, target, 'Xcode 工程标志，未验证目标 SDK')
        elif name == 'pubspec.yaml' and 'flutter' in (read(relative) or '').lower():
            frameworks.add('Flutter')
            for target in ('web', 'ios', 'android', 'macos', 'windows', 'linux'):
                directory = root / ('android' if target == 'android' else target)
                if directory.is_dir() and not directory.is_symlink():
                    marker(relative, target, 'Flutter 平台目录标志')
        elif name.endswith('.csproj'):
            text = (read(relative) or '').lower()
            frameworks.add('.NET')
            for hint, target in [('windows', 'windows'), ('maccatalyst', 'macos'), ('ios', 'ios'), ('android', 'android')]:
                if hint in text:
                    marker(relative, target, '.NET 目标框架标志')
            if 'microsoft.net.sdk.web' in text:
                marker(relative, 'api', '.NET Web SDK 标志')
        elif name in {'Cargo.toml', 'CMakeLists.txt'}:
            frameworks.add('Rust' if name == 'Cargo.toml' else 'CMake')

    def mapping(value):
        return value if isinstance(value, dict) else {}

    try:
        lock = json.loads(read('package-lock.json') or '{}')
    except ValueError:
        lock = {}
    required_present = all(relative in files and read(relative) is not None for relative in FOUNDATION_FILES)
    packages = mapping(mapping(lock).get('packages'))
    root_lock = mapping(packages.get(''))
    playable = mapping(packages.get('node_modules/@playwright/test'))
    foundation = Path(__file__).resolve().parents[1] / 'resources/web_api_starter'
    infrastructure = [p for p in FOUNDATION_FILES if p.startswith(('tooling/', 'tests/support/', 'tests/playwright.'))]
    try:
        fixed_contract = all(read(relative) is not None and signatures.get(relative)
            == 'sha256:' + hashlib.sha256((foundation / relative).read_bytes()).hexdigest() for relative in infrastructure)
    except OSError:
        fixed_contract = False
    node_requirement = mapping(package.get('engines')).get('node')
    supported = bool(required_present and package.get('type') == 'module' and not package.get('dependencies')
        and mapping(package.get('scripts')).get('build') == 'node tooling/build.mjs'
        and isinstance(node_requirement, str) and node_requirement in {'>=22.13.0', '>=22.13'}
        and mapping(package.get('devDependencies')).get('@playwright/test') == '1.63.0'
        and mapping(root_lock.get('devDependencies')).get('@playwright/test') == '1.63.0'
        and playable.get('version') == '1.63.0' and fixed_contract and not truncated
        and not (targets - {'web', 'api'}))
    if supported:
        targets.update({'web', 'api'})
        frameworks.add('AgentFlow Node Web/API')
    else:
        reasons.append({'code': 'unsupported_toolchain', 'message':
            '当前只支持具有固定构建、测试与启动契约的 AgentFlow Node Web/API 工程；检测到的其他工具链可登记，但暂不执行。'})
    if targets - {'web', 'api'}:
        reasons.append({'code': 'native_execution_unsupported', 'message': '原生平台目前可登记与识别，暂不支持自动构建和交付。'})
    if not targets:
        reasons.append({'code': 'project_type_unknown', 'message': '未找到足以确认产品类型的静态标志。'})
    if truncated:
        reasons.append({'code': 'static_scan_incomplete', 'message': '项目扫描达到范围或大小上限，不能确认执行契约。'})
    git = root / '.git'
    return {'project_path': str(root), 'diagnosis_kind': 'static', 'detected_targets': [t for t in TARGET_ORDER if t in targets],
        'markers': markers, 'frameworks': sorted(frameworks), 'git_detected': git.exists() and not git.is_symlink(),
        'execution_supported': supported, 'blocking_reasons': reasons, 'scan_truncated': truncated,
        'diagnosis_fingerprint': canonical_digest({'files': signatures, 'targets': sorted(targets), 'supported': supported}),
        'notice': '仅根据本地文件标志判断类型，未安装依赖、运行项目、调研市场或验证构建/测试。'}
