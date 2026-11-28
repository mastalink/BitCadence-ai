"""Code-first contract for plugin manifests and runtime documents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Text = Annotated[str, StringConstraints(max_length=1024)]
Argument = Annotated[str, StringConstraints(max_length=4096, pattern=r"^[^\x00\r\n]*$")]
Seconds = Annotated[int, Field(ge=1, le=31_536_000)]
Scalar = Union[str, int, float, bool]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Identity(Contract):
    instance: Name
    role: Name

    @model_validator(mode="after")
    def prohibit_owner_role(self) -> Identity:
        if self.role == "owner":
            raise ValueError("owner identity is reserved for granted actions")
        return self


class Command(Contract):
    argv: list[Argument] = Field(min_length=1, max_length=64)
    script_args: list[Annotated[int, Field(ge=1, le=63)]] = Field(
        default_factory=list, description="Additional zero-based argv indexes to resolve and pin as files."
    )
    channel: Optional[Name] = None

    @model_validator(mode="after")
    def valid_argv(self) -> Command:
        if not self.argv[0] or any(index >= len(self.argv) for index in self.script_args):
            raise ValueError("argv executable or script argument index is invalid")
        return self


class Task(Command):
    timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 60


class Periodic(Task):
    interval_seconds: Seconds


class Schedule(Periodic):
    catch_up: Literal["once"] = "once"
    overlap: Literal["skip"] = "skip"


class Parameter(Contract):
    name: Name
    type: Literal["string", "integer", "boolean"]
    choices: list[Argument] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_choices(self) -> Parameter:
        if self.choices and self.type != "string":
            raise ValueError("parameter choices require a string parameter")
        return self


class Action(Task):
    parameters: list[Parameter] = Field(default_factory=list, max_length=32)
    dangerous: bool = False

    @model_validator(mode="after")
    def distinct_names(self) -> Action:
        if len({item.name for item in self.parameters}) != len(self.parameters):
            raise ValueError("duplicate action parameter names")
        return self


class Component(Contract):
    type: Literal["heading", "text", "badge", "metric", "table", "action"]
    label: Text
    field: Optional[Name] = None
    action: Optional[Name] = None


class View(Contract):
    version: Literal["bitcadence.view/1"] = Field(alias="schema")
    title: Text
    components: list[Component] = Field(max_length=64)


class SecretState(Contract):
    present: bool
    expires_at: Optional[float] = Field(default=None, ge=0)


class Status(Contract):
    version: Literal["bitcadence.status/1"] = Field(alias="schema")
    health: Literal["ok", "degraded", "error"]
    message: Optional[Text] = None
    metrics: dict[Name, Scalar] = Field(default_factory=dict, max_length=64)
    needs_you: list[Text] = Field(default_factory=list, max_length=32)
    secrets: dict[Name, SecretState] = Field(default_factory=dict, max_length=32)


class Node(Contract):
    version: Literal["bitcadence.node/1"] = Field(alias="schema")
    id: Name
    endpoint: Text
    capabilities: dict[Name, Scalar] = Field(default_factory=dict, max_length=64)
    health: Literal["ok", "degraded", "offline", "unknown"] = "unknown"


class Event(Contract):
    type: Name
    state: Literal["started", "progress", "ok", "failed", "skipped"]
    progress: Optional[int] = Field(default=None, ge=0, le=100)


Commands = dict[str, Command]


class Manifest(Contract):
    version: Literal["bitcadence.plugin/1"] = Field(alias="schema")
    id: Name
    identity: Identity
    processes: dict[Name, Command] = Field(default_factory=dict, max_length=32)
    schedules: dict[Name, Schedule] = Field(default_factory=dict, max_length=64)
    status: Optional[Periodic] = None
    health: Optional[Periodic] = None
    actions: dict[Name, Action] = Field(default_factory=dict, max_length=64)
    views: dict[Name, View] = Field(default_factory=dict, max_length=16)
    job_title_prefixes: list[Text] = Field(default_factory=list, max_length=32)
    required_secrets: list[Name] = Field(default_factory=list, max_length=32)
    channels: list[Name] = Field(default_factory=list, max_length=32)
    resources: dict[Name, Scalar] = Field(
        default_factory=dict, max_length=32, description="Advisory only. The runtime does not enforce resources."
    )

    def commands(self) -> Commands:
        commands: Commands = {"process:" + key: value for key, value in self.processes.items()}
        commands.update({"schedule:" + key: value for key, value in self.schedules.items()})
        commands.update({"action:" + key: value for key, value in self.actions.items()})
        if self.status is not None:
            commands["status"] = self.status
        if self.health is not None:
            commands["health"] = self.health
        return commands


def schema_documents() -> dict[str, bytes]:
    contracts = {"manifest": Manifest, "status": Status, "view": View, "node": Node}
    return {
        name + ".schema.json": (json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **model.model_json_schema(by_alias=True),
        }, indent=2, sort_keys=True) + "\n").encode("utf-8")
        for name, model in contracts.items()
    }


def write_schemas(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in schema_documents().items():
        (directory / name).write_bytes(data)
