"""Démarre le worker workflows : découvre et sert les workflows du catalogue."""

import asyncio

from mistralai import workflows

from extract_workflows import EfrDetailsExtract, EfrListExtract, EfrProbe


async def main() -> None:
    classes = [EfrProbe, EfrListExtract, EfrDetailsExtract]
    names = [workflows.get_workflow_definition(cls).name for cls in classes]
    print("workflows served:", names, flush=True)
    await workflows.run_worker(classes)


if __name__ == "__main__":
    asyncio.run(main())
