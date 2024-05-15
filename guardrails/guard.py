import asyncio
import contextvars
import json
import os
import warnings
from copy import deepcopy
from string import Template
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Sequence,
    Type,
    Union,
    cast,
    overload,
)

from guardrails_api_client import (
    Guard as IGuard,
    ValidatorReference,
    ModelSchema,
    # AnyObject,
    # History,
    # HistoryEvent,
    ValidatePayload,
    # ValidationOutput,
    SimpleTypes,
)
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel, Field, PrivateAttr, field_validator
from typing_extensions import deprecated

from guardrails.api_client import GuardrailsApiClient
from guardrails.classes import OT, InputType, ValidationOutcome
from guardrails.classes.credentials import Credentials
from guardrails.classes.execution import GuardExecutionOptions
from guardrails.classes.generic import Stack
from guardrails.classes.history import Call
from guardrails.classes.history.call_inputs import CallInputs
from guardrails.classes.history.inputs import Inputs
from guardrails.classes.history.iteration import Iteration
from guardrails.classes.history.outputs import Outputs
from guardrails.classes.output_type import OutputTypes
from guardrails.classes.schema.processed_schema import ProcessedSchema
from guardrails.errors import ValidationError
from guardrails.llm_providers import (
    get_async_llm_ask,
    get_llm_api_enum,
    get_llm_ask,
    model_is_supported_server_side,
)
from guardrails.logger import logger, set_scope
from guardrails.prompt import Instructions, Prompt
from guardrails.rail import Rail
from guardrails.run import AsyncRunner, Runner, StreamRunner
from guardrails.schema import StringSchema
from guardrails.schema.pydantic_schema import pydantic_model_to_schema
from guardrails.schema.rail_schema import rail_file_to_schema, rail_string_to_schema
from guardrails.schema.validator import SchemaValidationError, validate_json_schema
from guardrails.stores.context import (
    Tracer,
    get_call_kwarg,
    get_tracer_context,
    set_call_kwargs,
    set_tracer,
    set_tracer_context,
)
from guardrails.utils.hub_telemetry_utils import HubTelemetry
from guardrails.utils.llm_response import LLMResponse
from guardrails.utils.reask_utils import FieldReAsk
from guardrails.utils.validator_utils import get_validator
from guardrails.validator_base import FailResult, Validator
from guardrails.types import (
    UseManyValidatorTuple,
    UseManyValidatorSpec,
    UseValidatorSpec,
)


class Guard(IGuard, Runnable, Generic[OT]):
    """The Guard class.

    This class is the main entry point for using Guardrails. It can be
    initialized by one of the following patterns:

    - `Guard().use(...)`
    - `Guard().use_many(...)`
    - `Guard.from_string(...)`
    - `Guard.from_pydantic(...)`
    - `Guard.from_rail(...)`
    - `Guard.from_rail_string(...)`

    The `__call__`
    method functions as a wrapper around LLM APIs. It takes in an LLM
    API, and optional prompt parameters, and returns a ValidationOutcome
    class that contains the raw output from
    the LLM, the validated output, as well as other helpful information.
    """

    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    validators: Optional[List[ValidatorReference]] = []
    schema: Optional[Dict[str, Any]] = None

    _num_reasks = None
    _tracer = None
    _tracer_context = None
    _hub_telemetry = None
    _guard_id = None
    _user_id = None
    _validators: List[Validator]
    _validator_map: Dict[str, List[Validator]] = {}
    _api_client: Optional[GuardrailsApiClient] = None
    _allow_metrics_collection: Optional[bool] = None
    _rail: Optional[Rail] = None
    _base_model: Optional[Union[Type[BaseModel], Type[List[Type[BaseModel]]]]]
    _exec_opts: Optional[GuardExecutionOptions] = PrivateAttr()

    def __init__(
        self,
        *,
        id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        validators: Optional[List[ValidatorReference]] = [],
        schema: Optional[Dict[str, Any]] = None,
        _exec_opts: Optional[GuardExecutionOptions] = None,
    ):
        """Initialize the Guard with optional Rail instance, num_reasks, and
        base_model."""

        # Shared Interface Properties
        self.id = id or str(id(self))
        self.name = name or f"gr-{self.id}"
        self.description = description
        self.schema = schema
        self._validator_map = {}
        super().__init__(
            id=self.id,
            name=self.name,
            description=self.description,
            validators=validators,
            var_schema=ModelSchema.from_dict(schema),
        )

        # if not rail:
        #     rail = (
        #         Rail.from_pydantic(base_model)
        #         if base_model
        #         else Rail.from_string_validators([])
        #     )
        # self.rail = rail
        # self.base_model = base_model

        # Backwards compatibility
        self._exec_opts = _exec_opts or GuardExecutionOptions()

        # TODO: Support a sink for history so that it is not solely held in memory
        self.history: Stack[Call] = Stack()

        # Legacy Guard.use() validators
        self._validators = []

        # Gaurdrails As A Service Initialization
        api_key = os.environ.get("GUARDRAILS_API_KEY")
        if api_key is not None:
            self._api_client = GuardrailsApiClient(api_key=api_key)
            self.upsert_guard()

    @field_validator("schema")
    @classmethod
    def must_be_valid_json_schema(
        cls, schema: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        if schema:
            try:
                validate_json_schema(schema)
            except SchemaValidationError as e:
                raise ValueError(f"{str(e)}\n{json.dumps(e.fields, indent=2)}")
        return schema

    def configure(
        self,
        *,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        allow_metrics_collection: Optional[bool] = None,
    ):
        """Configure the Guard."""
        if num_reasks:
            self._set_num_reasks(num_reasks)
        if tracer:
            self._set_tracer(tracer)
        self._configure_telemtry(allow_metrics_collection)

    def _set_num_reasks(self, num_reasks: int = 1):
        self._num_reasks = num_reasks

    def _set_tracer(self, tracer: Optional[Tracer] = None) -> None:
        self._tracer = tracer
        set_tracer(tracer)
        set_tracer_context()
        self._tracer_context = get_tracer_context()

    def _configure_telemtry(
        self, allow_metrics_collection: Optional[bool] = None
    ) -> None:
        if allow_metrics_collection is None:
            credentials = Credentials.from_rc_file(logger)
            allow_metrics_collection = credentials.no_metrics is False

        self._allow_metrics_collection = allow_metrics_collection

        if allow_metrics_collection:
            # Get unique id of user from credentials
            self._user_id = credentials.id or ""
            # Initialize Hub Telemetry singleton and get the tracer
            self._hub_telemetry = HubTelemetry()

    @classmethod
    def _from_rail_schema(
        cls,
        rail_schema: ProcessedSchema,
        rail: str,
        *,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        guard = cls(
            name=name,
            description=description,
            schema=rail_schema.json_schema,
            validators=rail_schema.validators,
            _exec_opts=rail_schema.exec_opts,
        )
        if rail_schema.output_type == OutputTypes.STRING:
            guard = cast(Guard[str], guard)
        elif rail_schema.output_type == OutputTypes.LIST:
            guard = cast(Guard[List], guard)
        else:
            guard = cast(Guard[Dict], guard)
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._rail = rail
        return guard

    @classmethod
    def from_rail(
        cls,
        rail_file: str,
        *,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Schema from a `.rail` file.

        Args:
            rail_file: The path to the `.rail` file.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.

        Returns:
            An instance of the `Guard` class.
        """  # noqa

        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        rail_schema = rail_file_to_schema(rail_file)
        return cls._from_rail_schema(
            rail_schema,
            rail=rail_file,
            num_reasks=num_reasks,
            tracer=tracer,
            name=name,
            description=description,
        )

    @classmethod
    def from_rail_string(
        cls,
        rail_string: str,
        *,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Schema from a `.rail` string.

        Args:
            rail_string: The `.rail` string.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.

        Returns:
            An instance of the `Guard` class.
        """  # noqa
        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        rail_schema = rail_string_to_schema(rail_string)
        return cls._from_rail_schema(
            rail_schema,
            rail=rail_string,
            num_reasks=num_reasks,
            tracer=tracer,
            name=name,
            description=description,
        )

    @classmethod
    def from_pydantic(
        cls,
        output_class: Union[Type[BaseModel], Type[List[Type[BaseModel]]]],
        *,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        num_reasks: Optional[int] = Field(
            default=None,
            deprecated=(
                "Setting num_reasks during initialization is deprecated"
                " and will be removed in 0.6.x!"
                "We recommend setting num_reasks when calling guard()"
                " or guard.parse() instead."
                "If you insist on setting it at the Guard level,"
                " use 'Guard.configure()'."
            ),
        ),
        reask_prompt: Optional[str] = None,
        reask_instructions: Optional[str] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Guard instance from a Pydantic model.

        Args:
            output_class: (Union[Type[BaseModel], List[Type[BaseModel]]]): The pydantic model that describes
            the desired structure of the output.
            prompt (str, optional): The prompt used to generate the string. Defaults to None.
            instructions (str, optional): Instructions for chat models. Defaults to None.
            reask_prompt (str, optional): An alternative prompt to use during reasks. Defaults to None.
            reask_instructions (str, optional): Alternative instructions to use during reasks. Defaults to None.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails. Deprecated
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.
        """  # noqa
        # We have to set the tracer in the ContextStore before the Rail,
        #   and therefore the Validators, are initialized
        cls._set_tracer(cls, tracer)  # type: ignore

        pydantic_schema = pydantic_model_to_schema(output_class)
        exec_opts = GuardExecutionOptions(
            prompt=prompt,
            instructions=instructions,
            reask_prompt=reask_prompt,
            reask_instructions=reask_instructions,
        )
        guard = cls(
            name=name,
            description=description,
            schema=pydantic_schema.json_schema,
            validators=pydantic_schema.validators,
            _exec_opts=exec_opts,
        )
        if pydantic_schema.output_type == OutputTypes.LIST:
            guard = cast(Guard[List], guard)
        else:
            guard = cast(Guard[Dict], guard)
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._base_model = output_class
        guard._validator_map = pydantic_schema.validator_map
        return guard

    @classmethod
    def from_string(
        cls,
        validators: Sequence[Validator],
        *,
        string_description: Optional[str] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        reask_prompt: Optional[str] = None,
        reask_instructions: Optional[str] = None,
        num_reasks: Optional[int] = None,
        tracer: Optional[Tracer] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Create a Guard instance for a string response.

        Args:
            validators: (List[Validator]): The list of validators to apply to the string output.
            string_description (str, optional): A description for the string to be generated. Defaults to None.
            prompt (str, optional): The prompt used to generate the string. Defaults to None.
            instructions (str, optional): Instructions for chat models. Defaults to None.
            reask_prompt (str, optional): An alternative prompt to use during reasks. Defaults to None.
            reask_instructions (str, optional): Alternative instructions to use during reasks. Defaults to None.
            num_reasks (int, optional): The max times to re-ask the LLM if validation fails.
            tracer (Tracer, optional): An OpenTelemetry tracer to use for metrics and traces. Defaults to None.
            name (str, optional): A unique name for this Guard. Defaults to `gr-` + the object id.
            description (str, optional): A description for this Guard. Defaults to None.
        """  # noqa

        # This might not be necessary anymore
        cls._set_tracer(cls, tracer)  # type: ignore

        validator_references = [
            ValidatorReference(
                id=v.rail_alias,
                on="$",
                on_fail=v.on_fail_descriptor,
                args=[],
                kwargs=v.get_args(),
            )
            for v in validators
        ]
        validator_map = {"$": validators}
        string_schema = ModelSchema(
            type=SimpleTypes.STRING, description=string_description
        )
        exec_opts = GuardExecutionOptions(
            prompt=prompt,
            instructions=instructions,
            reask_prompt=reask_prompt,
            reask_instructions=reask_instructions,
        )
        guard = cast(
            Guard[str],
            cls(
                name=name,
                description=description,
                schema=string_schema.to_dict(),
                validators=validator_references,
                _exec_opts=exec_opts,
            ),
        )
        guard.configure(num_reasks=num_reasks, tracer=tracer)
        guard._validator_map = validator_map
        return guard

    @overload
    def __call__(
        self,
        llm_api: Callable,
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        stream: Optional[bool] = False,
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]]: ...

    @overload
    def __call__(
        self,
        llm_api: Callable[[Any], Awaitable[Any]],
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Awaitable[ValidationOutcome[OT]]: ...

    def __call__(
        self,
        llm_api: Union[Callable, Callable[[Any], Awaitable[Any]]],
        *args,
        prompt_params: Optional[Dict] = None,
        num_reasks: Optional[int] = None,
        prompt: Optional[str] = None,
        instructions: Optional[str] = None,
        msg_history: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Union[
        Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]],
        Awaitable[ValidationOutcome[OT]],
    ]:
        """Call the LLM and validate the output. Pass an async LLM API to
        return a coroutine.

        Args:
            llm_api: The LLM API to call
                     (e.g. openai.Completion.create or openai.Completion.acreate)
            prompt_params: The parameters to pass to the prompt.format() method.
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt: The prompt to use for the LLM.
            instructions: Instructions for chat models.
            msg_history: The message history to pass to the LLM.
            metadata: Metadata to pass to the validators.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.
                               Defaults to `True` if a base model is provided,
                               `False` otherwise.

        Returns:
            The raw text output from the LLM and the validated output.
        """

        def __call(
            self,
            llm_api: Union[Callable, Callable[[Any], Awaitable[Any]]],
            *args,
            prompt_params: Optional[Dict] = None,
            num_reasks: Optional[int] = None,
            prompt: Optional[str] = None,
            instructions: Optional[str] = None,
            msg_history: Optional[List[Dict]] = None,
            metadata: Optional[Dict] = None,
            full_schema_reask: Optional[bool] = None,
            **kwargs,
        ):
            if metadata is None:
                metadata = {}
            if full_schema_reask is None:
                full_schema_reask = self._base_model is not None
            if prompt_params is None:
                prompt_params = {}

            if self._allow_metrics_collection:
                # Create a new span for this guard call
                self._hub_telemetry.create_new_span(
                    span_name="/guard_call",
                    attributes=[
                        ("guard_id", self.id),
                        ("user_id", self._user_id),
                        ("llm_api", llm_api.__name__ if llm_api else "None"),
                        ("custom_reask_prompt", self.reask_prompt is not None),
                        (
                            "custom_reask_instructions",
                            self.reask_instructions is not None,
                        ),
                    ],
                    is_parent=True,  # It will have children
                    has_parent=False,  # Has no parents
                )

            set_call_kwargs(kwargs)
            set_tracer(self._tracer)
            set_tracer_context(self._tracer_context)

            self.configure(num_reasks=num_reasks)
            if self._num_reasks is None:
                raise RuntimeError(
                    "`num_reasks` is `None` after calling `configure()`. "
                    "This should never happen."
                )

            input_prompt = prompt or (self.prompt._source if self.prompt else None)
            input_instructions = instructions or (
                self.instructions._source if self.instructions else None
            )
            call_inputs = CallInputs(
                llm_api=llm_api,
                prompt=input_prompt,
                instructions=input_instructions,
                msg_history=msg_history,
                prompt_params=prompt_params,
                num_reasks=self._num_reasks,
                metadata=metadata,
                full_schema_reask=full_schema_reask,
                args=list(args),
                kwargs=kwargs,
            )
            call_log = Call(inputs=call_inputs)
            set_scope(str(id(call_log)))
            self.history.push(call_log)

            if self._api_client is not None and model_is_supported_server_side(
                llm_api, *args, **kwargs
            ):
                return self._call_server(
                    llm_api=llm_api,
                    num_reasks=self._num_reasks,
                    prompt_params=prompt_params,
                    full_schema_reask=full_schema_reask,
                    call_log=call_log,
                    *args,
                    **kwargs,
                )

            # If the LLM API is async, return a coroutine
            if asyncio.iscoroutinefunction(llm_api):
                return self._call_async(
                    llm_api=llm_api,
                    prompt_params=prompt_params,
                    num_reasks=self._num_reasks,
                    prompt=prompt,
                    instructions=instructions,
                    msg_history=msg_history,
                    metadata=metadata,
                    full_schema_reask=full_schema_reask,
                    call_log=call_log,
                    *args,
                    **kwargs,
                )
            # Otherwise, call the LLM synchronously
            return self._call_sync(
                llm_api=llm_api,
                prompt_params=prompt_params,
                num_reasks=self._num_reasks,
                prompt=prompt,
                instructions=instructions,
                msg_history=msg_history,
                metadata=metadata,
                full_schema_reask=full_schema_reask,
                call_log=call_log,
                *args,
                **kwargs,
            )

        guard_context = contextvars.Context()
        return guard_context.run(
            __call,
            self,
            llm_api=llm_api,
            prompt_params=prompt_params,
            num_reasks=num_reasks,
            prompt=prompt,
            instructions=instructions,
            msg_history=msg_history,
            metadata=metadata,
            full_schema_reask=full_schema_reask,
            *args,
            **kwargs,
        )

    def _call_sync(
        self,
        llm_api: Callable,
        *args,
        call_log: Call,  # Not optional, but internal
        prompt_params: Optional[Dict],
        num_reasks: Optional[int],
        prompt: Optional[str],
        instructions: Optional[str],
        msg_history: Optional[List[Dict]],
        metadata: Optional[Dict],
        full_schema_reask: Optional[bool],
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Iterable[ValidationOutcome[OT]]]:
        instructions_obj = instructions or self.instructions
        prompt_obj = prompt or self.prompt
        msg_history_obj = msg_history or []
        if prompt_obj is None:
            if msg_history is not None and not len(msg_history_obj):
                raise RuntimeError(
                    "You must provide a prompt if msg_history is empty. "
                    "Alternatively, you can provide a prompt in the Schema constructor."
                )

        # Check whether stream is set
        if kwargs.get("stream", False):
            # If stream is True, use StreamRunner
            runner = StreamRunner(
                instructions=instructions_obj,
                prompt=prompt_obj,
                msg_history=msg_history_obj,
                api=get_llm_ask(llm_api, *args, **kwargs),
                prompt_schema=self.rail.prompt_schema,
                instructions_schema=self.rail.instructions_schema,
                msg_history_schema=self.rail.msg_history_schema,
                output_schema=self.output_schema,
                num_reasks=num_reasks,
                metadata=metadata,
                base_model=self._base_model,
                full_schema_reask=full_schema_reask,
                disable_tracer=(not self._allow_metrics_collection),
            )
            return runner(call_log=call_log, prompt_params=prompt_params)
        else:
            # Otherwise, use Runner
            runner = Runner(
                instructions=instructions_obj,
                prompt=prompt_obj,
                msg_history=msg_history_obj,
                api=get_llm_ask(llm_api, *args, **kwargs),
                prompt_schema=self.rail.prompt_schema,
                instructions_schema=self.rail.instructions_schema,
                msg_history_schema=self.rail.msg_history_schema,
                output_schema=self.output_schema,
                num_reasks=num_reasks,
                metadata=metadata,
                base_model=self._base_model,
                full_schema_reask=full_schema_reask,
                disable_tracer=(not self._allow_metrics_collection),
            )
            call = runner(call_log=call_log, prompt_params=prompt_params)
            return ValidationOutcome[OT].from_guard_history(call)

    async def _call_async(
        self,
        llm_api: Callable[[Any], Awaitable[Any]],
        *args,
        call_log: Call,
        prompt_params: Optional[Dict],
        num_reasks: Optional[int],
        prompt: Optional[str],
        instructions: Optional[str],
        msg_history: Optional[List[Dict]],
        metadata: Optional[Dict],
        full_schema_reask: Optional[bool],
        **kwargs,
    ) -> ValidationOutcome[OT]:
        """Call the LLM asynchronously and validate the output.

        Args:
            llm_api: The LLM API to call asynchronously (e.g. openai.Completion.acreate)
            prompt_params: The parameters to pass to the prompt.format() method.
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt: The prompt to use for the LLM.
            instructions: Instructions for chat models.
            msg_history: The message history to pass to the LLM.
            metadata: Metadata to pass to the validators.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.
                               Defaults to `True` if a base model is provided,
                               `False` otherwise.

        Returns:
            The raw text output from the LLM and the validated output.
        """
        instructions_obj = instructions or self.instructions
        prompt_obj = prompt or self.prompt
        msg_history_obj = msg_history or []
        if prompt_obj is None:
            if msg_history_obj is not None and not len(msg_history_obj):
                raise RuntimeError(
                    "You must provide a prompt if msg_history is empty. "
                    "Alternatively, you can provide a prompt in the RAIL spec."
                )

        runner = AsyncRunner(
            instructions=instructions_obj,
            prompt=prompt_obj,
            msg_history=msg_history_obj,
            api=get_async_llm_ask(llm_api, *args, **kwargs),
            prompt_schema=self.rail.prompt_schema,
            instructions_schema=self.rail.instructions_schema,
            msg_history_schema=self.rail.msg_history_schema,
            output_schema=self.output_schema,
            num_reasks=num_reasks,
            metadata=metadata,
            base_model=self._base_model,
            full_schema_reask=full_schema_reask,
            disable_tracer=(not self._allow_metrics_collection),
        )
        call = await runner.async_run(call_log=call_log, prompt_params=prompt_params)
        return ValidationOutcome[OT].from_guard_history(call)

    def __repr__(self):
        return f"Guard(RAIL={self.rail})"

    def __rich_repr__(self):
        yield "RAIL", self.rail

    def __stringify__(self):
        if self.rail and self.rail.output_type == "str":
            template = Template(
                """
                Guard {
                    validators: [
                        ${validators}
                    ]
                }
                    """
            )
            return template.safe_substitute(
                {
                    "validators": ",\n".join(
                        [v.__stringify__() for v in self._validators]
                    )
                }
            )
        return self.__repr__()

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: None = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> ValidationOutcome[OT]: ...

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable[[Any], Awaitable[Any]]] = ...,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Awaitable[ValidationOutcome[OT]]: ...

    @overload
    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> ValidationOutcome[OT]: ...

    def parse(
        self,
        llm_output: str,
        *args,
        metadata: Optional[Dict] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        full_schema_reask: Optional[bool] = None,
        **kwargs,
    ) -> Union[ValidationOutcome[OT], Awaitable[ValidationOutcome[OT]]]:
        """Alternate flow to using Guard where the llm_output is known.

        Args:
            llm_output: The output being parsed and validated.
            metadata: Metadata to pass to the validators.
            llm_api: The LLM API to call
                     (e.g. openai.Completion.create or openai.Completion.acreate)
            num_reasks: The max times to re-ask the LLM for invalid output.
            prompt_params: The parameters to pass to the prompt.format() method.
            full_schema_reask: When reasking, whether to regenerate the full schema
                               or just the incorrect values.

        Returns:
            The validated response. This is either a string or a dictionary,
                determined by the object schema defined in the RAILspec.
        """

        def __parse(
            self,
            llm_output: str,
            *args,
            metadata: Optional[Dict] = None,
            llm_api: Optional[Callable] = None,
            num_reasks: Optional[int] = None,
            prompt_params: Optional[Dict] = None,
            full_schema_reask: Optional[bool] = None,
            **kwargs,
        ):
            final_num_reasks = (
                num_reasks if num_reasks is not None else 0 if llm_api is None else None
            )

            if self._allow_metrics_collection:
                self._hub_telemetry.create_new_span(
                    span_name="/guard_parse",
                    attributes=[
                        ("guard_id", self.id),
                        ("user_id", self._user_id),
                        ("llm_api", llm_api.__name__ if llm_api else "None"),
                        ("custom_reask_prompt", self.reask_prompt is not None),
                        (
                            "custom_reask_instructions",
                            self.reask_instructions is not None,
                        ),
                    ],
                    is_parent=True,  # It will have children
                    has_parent=False,  # Has no parents
                )

            self.configure(num_reasks=final_num_reasks)
            if self._num_reasks is None:
                raise RuntimeError(
                    "`num_reasks` is `None` after calling `configure()`. "
                    "This should never happen."
                )
            if full_schema_reask is None:
                full_schema_reask = self._base_model is not None
            metadata = metadata or {}
            prompt_params = prompt_params or {}

            set_call_kwargs(kwargs)
            set_tracer(self._tracer)
            set_tracer_context(self._tracer_context)

            input_prompt = self.prompt._source if self.prompt else None
            input_instructions = (
                self.instructions._source if self.instructions else None
            )
            call_inputs = CallInputs(
                llm_api=llm_api,
                llm_output=llm_output,
                prompt=input_prompt,
                instructions=input_instructions,
                prompt_params=prompt_params,
                num_reasks=self._num_reasks,
                metadata=metadata,
                full_schema_reask=full_schema_reask,
                args=list(args),
                kwargs=kwargs,
            )
            call_log = Call(inputs=call_inputs)
            set_scope(str(id(call_log)))
            self.history.push(call_log)

            if self._api_client is not None and model_is_supported_server_side(
                llm_api, *args, **kwargs
            ):
                return self._call_server(
                    llm_output=llm_output,
                    llm_api=llm_api,
                    num_reasks=self._num_reasks,
                    prompt_params=prompt_params,
                    full_schema_reask=full_schema_reask,
                    call_log=call_log,
                    *args,
                    **kwargs,
                )

            # If the LLM API is async, return a coroutine
            if asyncio.iscoroutinefunction(llm_api):
                return self._async_parse(
                    llm_output=llm_output,
                    call_log=call_log,
                    metadata=metadata,
                    llm_api=llm_api,
                    num_reasks=self._num_reasks,
                    prompt_params=prompt_params,
                    full_schema_reask=full_schema_reask,
                    *args,
                    **kwargs,
                )
            # Otherwise, call the LLM synchronously
            return self._sync_parse(
                llm_output=llm_output,
                call_log=call_log,
                metadata=metadata,
                llm_api=llm_api,
                num_reasks=self._num_reasks,
                prompt_params=prompt_params,
                full_schema_reask=full_schema_reask,
                *args,
                **kwargs,
            )

        guard_context = contextvars.Context()
        return guard_context.run(
            __parse,
            self,
            llm_output=llm_output,
            metadata=metadata,
            llm_api=llm_api,
            num_reasks=num_reasks,
            prompt_params=prompt_params,
            full_schema_reask=full_schema_reask,
            *args,
            **kwargs,
        )

    def _sync_parse(
        self,
        llm_output: str,
        *args,
        call_log: Call,
        metadata: Dict,
        llm_api: Optional[Callable],
        num_reasks: int,
        prompt_params: Dict,
        full_schema_reask: bool,
        **kwargs,
    ) -> ValidationOutcome[OT]:
        """Alternate flow to using Guard where the llm_output is known.

        Args:
            llm_output: The output from the LLM.
            llm_api: The LLM API to use to re-ask the LLM.
            num_reasks: The max times to re-ask the LLM for invalid output.

        Returns:
            The validated response.
        """
        runner = Runner(
            instructions=kwargs.pop("instructions", None),
            prompt=kwargs.pop("prompt", None),
            msg_history=kwargs.pop("msg_history", None),
            api=get_llm_ask(llm_api, *args, **kwargs) if llm_api else None,
            prompt_schema=self.rail.prompt_schema,
            instructions_schema=self.rail.instructions_schema,
            msg_history_schema=self.rail.msg_history_schema,
            output_schema=self.output_schema,
            num_reasks=num_reasks,
            metadata=metadata,
            output=llm_output,
            base_model=self._base_model,
            full_schema_reask=full_schema_reask,
            disable_tracer=(not self._allow_metrics_collection),
        )
        call = runner(call_log=call_log, prompt_params=prompt_params)

        return ValidationOutcome[OT].from_guard_history(call)

    async def _async_parse(
        self,
        llm_output: str,
        *args,
        call_log: Call,
        metadata: Dict,
        llm_api: Optional[Callable[[Any], Awaitable[Any]]],
        num_reasks: int,
        prompt_params: Dict,
        full_schema_reask: bool,
        **kwargs,
    ) -> ValidationOutcome[OT]:
        """Alternate flow to using Guard where the llm_output is known.

        Args:
            llm_output: The output from the LLM.
            llm_api: The LLM API to use to re-ask the LLM.
            num_reasks: The max times to re-ask the LLM for invalid output.

        Returns:
            The validated response.
        """
        runner = AsyncRunner(
            instructions=kwargs.pop("instructions", None),
            prompt=kwargs.pop("prompt", None),
            msg_history=kwargs.pop("msg_history", None),
            api=get_async_llm_ask(llm_api, *args, **kwargs) if llm_api else None,
            prompt_schema=self.rail.prompt_schema,
            instructions_schema=self.rail.instructions_schema,
            msg_history_schema=self.rail.msg_history_schema,
            output_schema=self.output_schema,
            num_reasks=num_reasks,
            metadata=metadata,
            output=llm_output,
            base_model=self._base_model,
            full_schema_reask=full_schema_reask,
            disable_tracer=(not self._allow_metrics_collection),
        )
        call = await runner.async_run(call_log=call_log, prompt_params=prompt_params)

        return ValidationOutcome[OT].from_guard_history(call)

    @deprecated(
        """The `with_prompt_validation` method is deprecated,
        and will be removed in 0.5.x. Instead, please use
        `Guard().use(YourValidator, on='prompt')`.""",
        category=FutureWarning,
        stacklevel=2,
    )
    def with_prompt_validation(
        self,
        validators: Sequence[Validator],
    ):
        """Add prompt validation to the Guard.

        Args:
            validators: The validators to add to the prompt.
        """
        if self.rail.prompt_schema:
            warnings.warn("Overriding existing prompt validators.")
        schema = StringSchema.from_string(
            validators=validators,
        )
        self.rail.prompt_schema = schema
        return self

    @deprecated(
        """The `with_instructions_validation` method is deprecated,
        and will be removed in 0.5.x. Instead, please use
        `Guard().use(YourValidator, on='instructions')`.""",
        category=FutureWarning,
        stacklevel=2,
    )
    def with_instructions_validation(
        self,
        validators: Sequence[Validator],
    ):
        """Add instructions validation to the Guard.

        Args:
            validators: The validators to add to the instructions.
        """
        if self.rail.instructions_schema:
            warnings.warn("Overriding existing instructions validators.")
        schema = StringSchema.from_string(
            validators=validators,
        )
        self.rail.instructions_schema = schema
        return self

    @deprecated(
        """The `with_msg_history_validation` method is deprecated,
        and will be removed in 0.5.x. Instead, please use
        `Guard().use(YourValidator, on='msg_history')`.""",
        category=FutureWarning,
        stacklevel=2,
    )
    def with_msg_history_validation(
        self,
        validators: Sequence[Validator],
    ):
        """Add msg_history validation to the Guard.

        Args:
            validators: The validators to add to the msg_history.
        """
        if self.rail.msg_history_schema:
            warnings.warn("Overriding existing msg_history validators.")
        schema = StringSchema.from_string(
            validators=validators,
        )
        self.rail.msg_history_schema = schema
        return self

    def __add_validator(self, validator: Validator, on: str = "output"):
        # Only available for string output types
        if self.rail.output_type != "str":
            raise RuntimeError(
                "The `use` method is only available for string output types."
            )

        if on == "prompt":
            # If the prompt schema exists, add the validator to it
            if self.rail.prompt_schema:
                self.rail.prompt_schema.root_datatype.validators.append(validator)
            else:
                # Otherwise, create a new schema with the validator
                schema = StringSchema.from_string(
                    validators=[validator],
                )
                self.rail.prompt_schema = schema
        elif on == "instructions":
            # If the instructions schema exists, add the validator to it
            if self.rail.instructions_schema:
                self.rail.instructions_schema.root_datatype.validators.append(validator)
            else:
                # Otherwise, create a new schema with the validator
                schema = StringSchema.from_string(
                    validators=[validator],
                )
                self.rail.instructions_schema = schema
        elif on == "msg_history":
            # If the msg_history schema exists, add the validator to it
            if self.rail.msg_history_schema:
                self.rail.msg_history_schema.root_datatype.validators.append(validator)
            else:
                # Otherwise, create a new schema with the validator
                schema = StringSchema.from_string(
                    validators=[validator],
                )
                self.rail.msg_history_schema = schema
        elif on == "output":
            self._validators.append(validator)
            self.rail.output_schema.root_datatype.validators.append(validator)
        else:
            raise ValueError(
                """Invalid value for `on`. Must be one of the following:
                'output', 'prompt', 'instructions', 'msg_history'."""
            )

    @overload
    def use(self, validator: Validator, *, on: str = "output") -> "Guard": ...

    @overload
    def use(
        self, validator: Type[Validator], *args, on: str = "output", **kwargs
    ) -> "Guard": ...

    def use(
        self,
        validator: UseValidatorSpec,
        *args,
        on: str = "output",
        **kwargs,
    ) -> "Guard":
        """Use a validator to validate either of the following:
        - The output of an LLM request
        - The prompt
        - The instructions
        - The message history

        *Note*: For on="output", `use` is only available for string output types.

        Args:
            validator: The validator to use. Either the class or an instance.
            on: The part of the LLM request to validate. Defaults to "output".
        """
        hydrated_validator = get_validator(validator, *args, **kwargs)
        self.__add_validator(hydrated_validator, on=on)
        return self

    @overload
    def use_many(self, *validators: Validator, on: str = "output") -> "Guard": ...

    @overload
    def use_many(
        self,
        *validators: UseManyValidatorTuple,
        on: str = "output",
    ) -> "Guard": ...

    def use_many(
        self,
        *validators: UseManyValidatorSpec,
        on: str = "output",
    ) -> "Guard":
        """Use a validator to validate results of an LLM request.

        *Note*: `use_many` is only available for string output types.
        """
        if self.rail.output_type != "str":
            raise RuntimeError(
                "The `use_many` method is only available for string output types."
            )

        # Loop through the validators
        for v in validators:
            hydrated_validator = get_validator(v)
            self.__add_validator(hydrated_validator, on=on)
        return self

    def validate(self, llm_output: str, *args, **kwargs) -> ValidationOutcome[str]:
        if (
            not self.rail
            or self.rail.output_schema.root_datatype.validators != self._validators
        ):
            self.rail = Rail.from_string_validators(
                validators=self._validators,
                prompt=self.prompt.source if self.prompt else None,
                instructions=self.instructions.source if self.instructions else None,
                reask_prompt=self.reask_prompt.source if self.reask_prompt else None,
                reask_instructions=self.reask_instructions.source
                if self.reask_instructions
                else None,
            )

        return self.parse(llm_output=llm_output, *args, **kwargs)

    # No call support for this until
    # https://github.com/guardrails-ai/guardrails/pull/525 is merged
    # def __call__(self, llm_output: str, *args, **kwargs) -> ValidationOutcome[str]:
    #     return self.validate(llm_output, *args, **kwargs)

    def invoke(
        self, input: InputType, config: Optional[RunnableConfig] = None
    ) -> InputType:
        output = BaseMessage(content="", type="")
        str_input = None
        input_is_chat_message = False
        if isinstance(input, BaseMessage):
            input_is_chat_message = True
            str_input = str(input.content)
            output = deepcopy(input)
        else:
            str_input = str(input)

        response = self.validate(str_input)

        validated_output = response.validated_output
        if not validated_output:
            raise ValidationError(
                (
                    "The response from the LLM failed validation!"
                    "See `guard.history` for more details."
                )
            )

        if isinstance(validated_output, Dict):
            validated_output = json.dumps(validated_output)

        if input_is_chat_message:
            output.content = validated_output
            return cast(InputType, output)
        return cast(InputType, validated_output)

    def _to_request(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "railspec": self.rail._to_request(),
            "numReasks": self._num_reasks,
        }

    def upsert_guard(self):
        if self._api_client:
            guard_dict = self._to_request()
            self._api_client.upsert_guard(IGuard.from_dict(guard_dict))
        else:
            raise ValueError("Guard does not have an api client!")

    def _call_server(
        self,
        *args,
        llm_output: Optional[str] = None,
        llm_api: Optional[Callable] = None,
        num_reasks: Optional[int] = None,
        prompt_params: Optional[Dict] = None,
        metadata: Optional[Dict] = {},
        full_schema_reask: Optional[bool] = True,
        call_log: Optional[Call],
        # prompt: Optional[str],
        # instructions: Optional[str],
        # msg_history: Optional[List[Dict]],
        **kwargs,
    ):
        if self._api_client:
            payload: Dict[str, Any] = {"args": list(args)}
            payload.update(**kwargs)
            if llm_output is not None:
                payload["llmOutput"] = llm_output
            if num_reasks is not None:
                payload["numReasks"] = num_reasks
            if prompt_params is not None:
                payload["promptParams"] = prompt_params
            if llm_api is not None:
                payload["llmApi"] = get_llm_api_enum(llm_api)
            # TODO: get enum for llm_api
            validation_output: Optional[Any] = self._api_client.validate(
                guard=self,  # type: ignore
                payload=ValidatePayload.from_dict(payload),
                openai_api_key=get_call_kwarg("api_key"),
            )

            if not validation_output:
                return ValidationOutcome[OT](
                    raw_llm_output=None,
                    validated_output=None,
                    validation_passed=False,
                    error="The response from the server was empty!",
                )

            call_log = call_log or Call()
            if llm_api is not None:
                llm_api = get_llm_ask(llm_api)
                if asyncio.iscoroutinefunction(llm_api):
                    llm_api = get_async_llm_ask(llm_api)
            session_history = (
                validation_output.session_history
                if validation_output is not None and validation_output.session_history
                else []
            )
            history: List[Call]
            for history in session_history:
                history_events: Optional[List[Any]] = (  # type: ignore
                    history.history
                )
                if history_events is None:
                    continue

                iterations = [
                    Iteration(
                        inputs=Inputs(
                            llm_api=llm_api,
                            llm_output=llm_output,
                            instructions=(
                                Instructions(h.instructions) if h.instructions else None
                            ),
                            prompt=(
                                Prompt(h.prompt.source)  # type: ignore
                                if h.prompt
                                else None
                            ),
                            prompt_params=prompt_params,
                            num_reasks=(num_reasks or 0),
                            metadata=metadata,
                            full_schema_reask=full_schema_reask,
                        ),
                        outputs=Outputs(
                            llm_response_info=LLMResponse(
                                output=h.output  # type: ignore
                            ),
                            raw_output=h.output,
                            parsed_output=(
                                h.parsed_output.to_dict()
                                if isinstance(h.parsed_output, Any)
                                else h.parsed_output
                            ),
                            validation_output=(
                                h.validated_output.to_dict()
                                if isinstance(h.validated_output, Any)
                                else h.validated_output
                            ),
                            reasks=list(
                                [
                                    FieldReAsk(
                                        incorrect_value=r.to_dict().get(
                                            "incorrect_value"
                                        ),
                                        path=r.to_dict().get("path"),
                                        fail_results=[
                                            FailResult(
                                                error_message=r.to_dict().get(
                                                    "error_message"
                                                ),
                                                fix_value=r.to_dict().get("fix_value"),
                                            )
                                        ],
                                    )
                                    for r in h.reasks  # type: ignore
                                ]
                                if h.reasks is not None
                                else []
                            ),
                        ),
                    )
                    for h in history_events
                ]
                call_log.iterations.extend(iterations)
                if self.history.length == 0:
                    self.history.push(call_log)

            # Our interfaces are too different for this to work right now.
            # Once we move towards shared interfaces for both the open source
            # and the api we can re-enable this.
            # return ValidationOutcome[OT].from_guard_history(call_log)
            return ValidationOutcome[OT](
                raw_llm_output=validation_output.raw_llm_response,  # type: ignore
                validated_output=cast(OT, validation_output.validated_output),
                validation_passed=validation_output.result,
            )
        else:
            raise ValueError("Guard does not have an api client!")
