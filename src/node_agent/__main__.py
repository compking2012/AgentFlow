from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path

from agentflow.execution.capabilities import probe_target
from agentflow.execution.models import TargetConfig
from agentflow.execution.pki import create_node_key_and_csr
from node_agent.client import enroll_node
from node_agent.daemon import NodeDaemon


def main() -> None:
    parser = argparse.ArgumentParser(description="Owner-paired AgentFlow build/test node (no LLM credentials)")
    parser.add_argument("--data-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--label", default="AgentFlow node")
    pairing = sub.add_parser("pair")
    pairing.add_argument("--label", default="AgentFlow node")
    pairing.add_argument("--origin", required=True)
    pairing.add_argument("--pairing-id", required=True)
    pairing.add_argument("--controller-pin", required=True)
    for command in ["probe", "run"]:
        cmd = sub.add_parser(command)
        cmd.add_argument("--targets", type=Path, required=True, help="Owner-approved TargetConfig JSON array")
        if command == "run":
            cmd.add_argument("--once", action="store_true")
            cmd.add_argument("--trusted-project-mode", action="store_true",
                             help="Explicitly allow only trusted project code on this host; not a verified security sandbox")
            cmd.add_argument("--resource-id", action="append", default=[])
    args = parser.parse_args()
    if args.command == "init":
        csr, digest = create_node_key_and_csr(args.data_dir, args.label)
        (args.data_dir / "node.csr").write_text(csr)
        print(json.dumps({"public_key_fingerprint": digest, "csr_path": str(args.data_dir / "node.csr")}))
        return

    async def run():
        if args.command == "pair":
            code = getpass.getpass("Single-use pairing code (not written to command arguments): ")
            result = await enroll_node(args.data_dir, origin=args.origin, pairing_id=args.pairing_id,
                                      single_use_code=code, controller_fingerprint=args.controller_pin, label=args.label)
            print(json.dumps({"node_id": result["node_id"], "state": "paired"}))
            return
        raw = json.loads(args.targets.read_text())
        targets = [TargetConfig.model_validate(value) for value in (raw if isinstance(raw, list) else [raw])]
        if args.command == "probe":
            for target in targets:
                print((await probe_target(target)).model_dump_json())
            return
        daemon = NodeDaemon(args.data_dir, targets, trusted_project_mode=args.trusted_project_mode, resource_ids=args.resource_id)
        try:
            await daemon.initialize()
            while True:
                result = await daemon.run_once()
                print(json.dumps(result), flush=True)
                if args.once or result["state"] == "execution_unknown":
                    break
                await asyncio.sleep(3)
        finally:
            await daemon.close()
    asyncio.run(run())


if __name__ == "__main__":
    main()
