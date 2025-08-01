import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from autogpt_libs.utils.cache import thread_cached

import backend.blocks.llm as llm
from backend.blocks.agent import AgentExecutorBlock
from backend.data.block import (
    Block,
    BlockCategory,
    BlockInput,
    BlockOutput,
    BlockSchema,
    BlockType,
)
from backend.data.model import NodeExecutionStats, SchemaField
from backend.util import json

if TYPE_CHECKING:
    from backend.data.graph import Link, Node

logger = logging.getLogger(__name__)


@thread_cached
def get_database_manager_client():
    from backend.executor import DatabaseManagerAsyncClient
    from backend.util.service import get_service_client

    return get_service_client(DatabaseManagerAsyncClient, health_check=False)


def _get_tool_requests(entry: dict[str, Any]) -> list[str]:
    """
    Return a list of tool_call_ids if the entry is a tool request.
    Supports both OpenAI and Anthropics formats.
    """
    tool_call_ids = []
    if entry.get("role") != "assistant":
        return tool_call_ids

    # OpenAI: check for tool_calls in the entry.
    calls = entry.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if tool_id := call.get("id"):
                tool_call_ids.append(tool_id)

    # Anthropics: check content items for tool_use type.
    content = entry.get("content")
    if isinstance(content, list):
        for item in content:
            if item.get("type") != "tool_use":
                continue
            if tool_id := item.get("id"):
                tool_call_ids.append(tool_id)

    return tool_call_ids


def _get_tool_responses(entry: dict[str, Any]) -> list[str]:
    """
    Return a list of tool_call_ids if the entry is a tool response.
    Supports both OpenAI and Anthropics formats.
    """
    tool_call_ids: list[str] = []

    # OpenAI: a tool response message with role "tool" and key "tool_call_id".
    if entry.get("role") == "tool":
        if tool_call_id := entry.get("tool_call_id"):
            tool_call_ids.append(str(tool_call_id))

    # Anthropics: check content items for tool_result type.
    if entry.get("role") == "user":
        content = entry.get("content")
        if isinstance(content, list):
            for item in content:
                if item.get("type") != "tool_result":
                    continue
                if tool_call_id := item.get("tool_use_id"):
                    tool_call_ids.append(tool_call_id)

    return tool_call_ids


def _create_tool_response(call_id: str, output: Any) -> dict[str, Any]:
    """
    Create a tool response message for either OpenAI or Anthropics,
    based on the tool_id format.
    """
    content = output if isinstance(output, str) else json.dumps(output)

    # Anthropics format: tool IDs typically start with "toolu_"
    if call_id.startswith("toolu_"):
        return {
            "role": "user",
            "type": "message",
            "content": [
                {"tool_use_id": call_id, "type": "tool_result", "content": content}
            ],
        }

    # OpenAI format: tool IDs typically start with "call_".
    # Or default fallback (if the tool_id doesn't match any known prefix)
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def get_pending_tool_calls(conversation_history: list[Any]) -> dict[str, int]:
    """
    All the tool calls entry in the conversation history requires a response.
    This function returns the pending tool calls that has not generated an output yet.

    Return: dict[str, int] - A dictionary of pending tool call IDs with their count.
    """
    pending_calls = Counter()
    for history in conversation_history:
        for call_id in _get_tool_requests(history):
            pending_calls[call_id] += 1

        for call_id in _get_tool_responses(history):
            pending_calls[call_id] -= 1

    return {call_id: count for call_id, count in pending_calls.items() if count > 0}


class SmartDecisionMakerBlock(Block):
    """
    A block that uses a language model to make smart decisions based on a given prompt.
    """

    class Input(BlockSchema):
        prompt: str = SchemaField(
            description="The prompt to send to the language model.",
            placeholder="Enter your prompt here...",
        )
        model: llm.LlmModel = SchemaField(
            title="LLM Model",
            default=llm.LlmModel.GPT4O,
            description="The language model to use for answering the prompt.",
            advanced=False,
        )
        credentials: llm.AICredentials = llm.AICredentialsField()
        multiple_tool_calls: bool = SchemaField(
            title="Multiple Tool Calls",
            default=False,
            description="Whether to allow multiple tool calls in a single response.",
            advanced=True,
        )
        sys_prompt: str = SchemaField(
            title="System Prompt",
            default="Thinking carefully step by step decide which function to call. "
            "Always choose a function call from the list of function signatures, "
            "and always provide the complete argument provided with the type "
            "matching the required jsonschema signature, no missing argument is allowed. "
            "If you have already completed the task objective, you can end the task "
            "by providing the end result of your work as a finish message. "
            "Function parameters that has no default value and not optional typed has to be provided. ",
            description="The system prompt to provide additional context to the model.",
        )
        conversation_history: list[dict] = SchemaField(
            default_factory=list,
            description="The conversation history to provide context for the prompt.",
        )
        last_tool_output: Any = SchemaField(
            default=None,
            description="The output of the last tool that was called.",
        )
        retry: int = SchemaField(
            title="Retry Count",
            default=3,
            description="Number of times to retry the LLM call if the response does not match the expected format.",
        )
        prompt_values: dict[str, str] = SchemaField(
            advanced=False,
            default_factory=dict,
            description="Values used to fill in the prompt. The values can be used in the prompt by putting them in a double curly braces, e.g. {{variable_name}}.",
        )
        max_tokens: int | None = SchemaField(
            advanced=True,
            default=None,
            description="The maximum number of tokens to generate in the chat completion.",
        )
        ollama_host: str = SchemaField(
            advanced=True,
            default="localhost:11434",
            description="Ollama host for local  models",
        )

        @classmethod
        def get_missing_links(cls, data: BlockInput, links: list["Link"]) -> set[str]:
            # conversation_history & last_tool_output validation is handled differently
            missing_links = super().get_missing_links(
                data,
                [
                    link
                    for link in links
                    if link.sink_name
                    not in ["conversation_history", "last_tool_output"]
                ],
            )

            # Avoid executing the block if the last_tool_output is connected to a static
            # link, like StoreValueBlock or AgentInputBlock.
            if any(link.sink_name == "conversation_history" for link in links) and any(
                link.sink_name == "last_tool_output" and link.is_static
                for link in links
            ):
                raise ValueError(
                    "Last Tool Output can't be connected to a static (dashed line) "
                    "link like the output of `StoreValue` or `AgentInput` block"
                )

            # Check that both conversation_history and last_tool_output are connected together
            if any(link.sink_name == "conversation_history" for link in links) != any(
                link.sink_name == "last_tool_output" for link in links
            ):
                raise ValueError(
                    "Last Tool Output is needed when Conversation History is used, "
                    "and vice versa. Please connect both inputs together."
                )

            return missing_links

        @classmethod
        def get_missing_input(cls, data: BlockInput) -> set[str]:
            if missing_input := super().get_missing_input(data):
                return missing_input

            conversation_history = data.get("conversation_history", [])
            pending_tool_calls = get_pending_tool_calls(conversation_history)
            last_tool_output = data.get("last_tool_output")

            # Tool call is pending, wait for the tool output to be provided.
            if last_tool_output is None and pending_tool_calls:
                return {"last_tool_output"}

            # No tool call is pending, wait for the conversation history to be updated.
            if last_tool_output is not None and not pending_tool_calls:
                return {"conversation_history"}

            return set()

    class Output(BlockSchema):
        error: str = SchemaField(description="Error message if the API call failed.")
        tools: Any = SchemaField(description="The tools that are available to use.")
        finished: str = SchemaField(
            description="The finished message to display to the user."
        )
        conversations: list[Any] = SchemaField(
            description="The conversation history to provide context for the prompt."
        )

    def __init__(self):
        super().__init__(
            id="3b191d9f-356f-482d-8238-ba04b6d18381",
            description="Uses AI to intelligently decide what tool to use.",
            categories={BlockCategory.AI},
            block_type=BlockType.AI,
            input_schema=SmartDecisionMakerBlock.Input,
            output_schema=SmartDecisionMakerBlock.Output,
            test_input={
                "prompt": "Hello, World!",
                "credentials": llm.TEST_CREDENTIALS_INPUT,
            },
            test_output=[],
            test_credentials=llm.TEST_CREDENTIALS,
        )

    @staticmethod
    def cleanup(s: str):
        return re.sub(r"[^a-zA-Z0-9_-]", "_", s).lower()

    @staticmethod
    async def _create_block_function_signature(
        sink_node: "Node", links: list["Link"]
    ) -> dict[str, Any]:
        """
        Creates a function signature for a block node.

        Args:
            sink_node: The node for which to create a function signature.
            links: The list of links connected to the sink node.

        Returns:
            A dictionary representing the function signature in the format expected by LLM tools.

        Raises:
            ValueError: If the block specified by sink_node.block_id is not found.
        """
        block = sink_node.block

        tool_function: dict[str, Any] = {
            "name": SmartDecisionMakerBlock.cleanup(block.name),
            "description": block.description,
        }
        sink_block_input_schema = block.input_schema
        properties = {}

        for link in links:
            sink_name = SmartDecisionMakerBlock.cleanup(link.sink_name)
            properties[sink_name] = sink_block_input_schema.get_field_schema(
                link.sink_name
            )

        tool_function["parameters"] = {
            **block.input_schema.jsonschema(),
            "properties": properties,
        }

        return {"type": "function", "function": tool_function}

    @staticmethod
    async def _create_agent_function_signature(
        sink_node: "Node", links: list["Link"]
    ) -> dict[str, Any]:
        """
        Creates a function signature for an agent node.

        Args:
            sink_node: The agent node for which to create a function signature.
            links: The list of links connected to the sink node.

        Returns:
            A dictionary representing the function signature in the format expected by LLM tools.

        Raises:
            ValueError: If the graph metadata for the specified graph_id and graph_version is not found.
        """
        graph_id = sink_node.input_default.get("graph_id")
        graph_version = sink_node.input_default.get("graph_version")
        if not graph_id or not graph_version:
            raise ValueError("Graph ID or Graph Version not found in sink node.")

        db_client = get_database_manager_client()
        sink_graph_meta = await db_client.get_graph_metadata(graph_id, graph_version)
        if not sink_graph_meta:
            raise ValueError(
                f"Sink graph metadata not found: {graph_id} {graph_version}"
            )

        tool_function: dict[str, Any] = {
            "name": SmartDecisionMakerBlock.cleanup(sink_graph_meta.name),
            "description": sink_graph_meta.description,
        }

        properties = {}

        for link in links:
            sink_block_input_schema = sink_node.input_default["input_schema"]
            sink_block_properties = sink_block_input_schema.get("properties", {}).get(
                link.sink_name, {}
            )
            sink_name = SmartDecisionMakerBlock.cleanup(link.sink_name)
            description = (
                sink_block_properties["description"]
                if "description" in sink_block_properties
                else f"The {link.sink_name} of the tool"
            )
            properties[sink_name] = {
                "type": "string",
                "description": description,
                "default": json.dumps(sink_block_properties.get("default", None)),
            }

        tool_function["parameters"] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "strict": True,
        }

        return {"type": "function", "function": tool_function}

    @staticmethod
    async def _create_function_signature(node_id: str) -> list[dict[str, Any]]:
        """
        Creates function signatures for tools linked to a specified node within a graph.

        This method filters the graph links to identify those that are tools and are
        connected to the given node_id. It then constructs function signatures for each
        tool based on the metadata and input schema of the linked nodes.

        Args:
            node_id: The node_id for which to create function signatures.

        Returns:
            list[dict[str, Any]]: A list of dictionaries, each representing a function signature
                                  for a tool, including its name, description, and parameters.

        Raises:
            ValueError: If no tool links are found for the specified node_id, or if a sink node
                        or its metadata cannot be found.
        """
        db_client = get_database_manager_client()
        tools = [
            (link, node)
            for link, node in await db_client.get_connected_output_nodes(node_id)
            if link.source_name.startswith("tools_^_") and link.source_id == node_id
        ]
        if not tools:
            raise ValueError("There is no next node to execute.")

        return_tool_functions: list[dict[str, Any]] = []

        grouped_tool_links: dict[str, tuple["Node", list["Link"]]] = {}
        for link, node in tools:
            if link.sink_id not in grouped_tool_links:
                grouped_tool_links[link.sink_id] = (node, [link])
            else:
                grouped_tool_links[link.sink_id][1].append(link)

        for sink_node, links in grouped_tool_links.values():
            if not sink_node:
                raise ValueError(f"Sink node not found: {links[0].sink_id}")

            if sink_node.block_id == AgentExecutorBlock().id:
                return_tool_functions.append(
                    await SmartDecisionMakerBlock._create_agent_function_signature(
                        sink_node, links
                    )
                )
            else:
                return_tool_functions.append(
                    await SmartDecisionMakerBlock._create_block_function_signature(
                        sink_node, links
                    )
                )

        return return_tool_functions

    async def run(
        self,
        input_data: Input,
        *,
        credentials: llm.APIKeyCredentials,
        graph_id: str,
        node_id: str,
        graph_exec_id: str,
        node_exec_id: str,
        user_id: str,
        **kwargs,
    ) -> BlockOutput:
        tool_functions = await self._create_function_signature(node_id)
        yield "tool_functions", json.dumps(tool_functions)

        input_data.conversation_history = input_data.conversation_history or []
        prompt = [json.to_dict(p) for p in input_data.conversation_history if p]

        pending_tool_calls = get_pending_tool_calls(input_data.conversation_history)
        if pending_tool_calls and input_data.last_tool_output is None:
            raise ValueError(f"Tool call requires an output for {pending_tool_calls}")

        # Only assign the last tool output to the first pending tool call
        tool_output = []
        if pending_tool_calls and input_data.last_tool_output is not None:
            # Get the first pending tool call ID
            first_call_id = next(iter(pending_tool_calls.keys()))
            tool_output.append(
                _create_tool_response(first_call_id, input_data.last_tool_output)
            )

            # Add tool output to prompt right away
            prompt.extend(tool_output)

            # Check if there are still pending tool calls after handling the first one
            remaining_pending_calls = get_pending_tool_calls(prompt)

            # If there are still pending tool calls, yield the conversation and return early
            if remaining_pending_calls:
                yield "conversations", prompt
                return

        # Fallback on adding tool output in the conversation history as user prompt.
        elif input_data.last_tool_output:
            logger.error(
                f"[SmartDecisionMakerBlock-node_exec_id={node_exec_id}] "
                f"No pending tool calls found. This may indicate an issue with the "
                f"conversation history, or the tool giving response more than once."
                f"This should not happen! Please check the conversation history for any inconsistencies."
            )
            tool_output.append(
                {
                    "role": "user",
                    "content": f"Last tool output: {json.dumps(input_data.last_tool_output)}",
                }
            )
            prompt.extend(tool_output)
        if input_data.multiple_tool_calls:
            input_data.sys_prompt += "\nYou can call a tool (different tools) multiple times in a single response."
        else:
            input_data.sys_prompt += "\nOnly provide EXACTLY one function call, multiple tool calls is strictly prohibited."

        values = input_data.prompt_values
        if values:
            input_data.prompt = llm.fmt.format_string(input_data.prompt, values)
            input_data.sys_prompt = llm.fmt.format_string(input_data.sys_prompt, values)

        prefix = "[Main Objective Prompt]: "

        if input_data.sys_prompt and not any(
            p["role"] == "system" and p["content"].startswith(prefix) for p in prompt
        ):
            prompt.append({"role": "system", "content": prefix + input_data.sys_prompt})

        if input_data.prompt and not any(
            p["role"] == "user" and p["content"].startswith(prefix) for p in prompt
        ):
            prompt.append({"role": "user", "content": prefix + input_data.prompt})

        response = await llm.llm_call(
            credentials=credentials,
            llm_model=input_data.model,
            prompt=prompt,
            json_format=False,
            max_tokens=input_data.max_tokens,
            tools=tool_functions,
            ollama_host=input_data.ollama_host,
            parallel_tool_calls=input_data.multiple_tool_calls,
        )

        # Track LLM usage stats
        self.merge_stats(
            NodeExecutionStats(
                input_token_count=response.prompt_tokens,
                output_token_count=response.completion_tokens,
                llm_call_count=1,
            )
        )

        if not response.tool_calls:
            yield "finished", response.response
            return

        for tool_call in response.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)

            # Find the tool definition to get the expected arguments
            tool_def = next(
                (
                    tool
                    for tool in tool_functions
                    if tool["function"]["name"] == tool_name
                ),
                None,
            )

            if (
                tool_def
                and "function" in tool_def
                and "parameters" in tool_def["function"]
            ):
                expected_args = tool_def["function"]["parameters"].get("properties", {})
            else:
                expected_args = tool_args.keys()

            # Yield provided arguments and None for missing ones
            for arg_name in expected_args:
                if arg_name in tool_args:
                    yield f"tools_^_{tool_name}_~_{arg_name}", tool_args[arg_name]
                else:
                    yield f"tools_^_{tool_name}_~_{arg_name}", None

        # Add reasoning to conversation history if available
        if response.reasoning:
            prompt.append(
                {"role": "assistant", "content": f"[Reasoning]: {response.reasoning}"}
            )

        prompt.append(response.raw_response)
        yield "conversations", prompt
