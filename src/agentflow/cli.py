"""Five everyday commands; configuration has one fixed, private file."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agentflow import __version__
from agentflow.common import DomainError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='agentflow', description='输入目标，创建并交付软件产品。',
        epilog='配置文件：~/.config/agentflow/config.toml（模型、产品类型、人审、限额和运行环境）',
        allow_abbrev=False)
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('start', help='启动平台并打开工作台', allow_abbrev=False)
    run = commands.add_parser('run', help='根据目标开始研发产品', allow_abbrev=False)
    run.add_argument('goal', help='用一句话或一段文字描述产品目标')
    run.add_argument('--name', help='产品名称，默认从目标生成')
    run.add_argument('--output', type=Path, help='本次产品的输出目录')
    status = commands.add_parser('status', help='查看全部产品或指定产品进度', allow_abbrev=False)
    status.add_argument('product_id', nargs='?', help='可选的产品ID')
    launch = commands.add_parser('launch', help='启动已交付产品', allow_abbrev=False)
    launch.add_argument('product_id', help='产品ID')
    stop = commands.add_parser('stop', help='停止平台，或停止指定产品的预览', allow_abbrev=False)
    stop.add_argument('product_id', nargs='?', help='省略时停止平台')
    return parser


def main():
    # Help/version never read credentials or create files.
    args = build_parser().parse_args()
    from agentflow.configuration import load_configuration
    from agentflow.product_cli import run_product_command
    try:
        try:
            configuration = load_configuration(create=True)
        except DomainError:
            # A mistyped edit must not prevent stopping the running controller.
            from types import SimpleNamespace

            from agentflow.configuration import configuration_path
            from agentflow.control.instance import active_data_dir
            from agentflow.settings import Settings
            directory = active_data_dir() if args.command in {'status', 'stop'} else None
            if directory is None:
                raise
            configuration = SimpleNamespace(settings=Settings(data_dir=directory), config_path=configuration_path())
        asyncio.run(run_product_command(args, configuration))
    except (DomainError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == '__main__':
    main()
