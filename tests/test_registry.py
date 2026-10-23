import pytest

from app.tools import registry


@pytest.fixture(autouse=True)
def isolate_registry():
    """Keep test tools separate from tools registered by the application."""
    original_tools = registry.TOOLS.copy()
    registry.TOOLS.clear()
    yield
    registry.TOOLS.clear()
    registry.TOOLS.update(original_tools)


def test_tool_registers_function_and_schema():
    @registry.tool
    def greet(name: str) -> str:
        """Greet a person.

        Args:
            name: Person to greet
        """
        return f"Hello, {name}!"

    assert "greet" in registry.TOOLS
    assert registry.TOOLS["greet"]["fn"] is greet

    schema = registry.get_schemas()[0]["function"]
    assert schema["name"] == "greet"
    assert schema["description"] == "Greet a person."
    assert schema["parameters"]["properties"]["name"] == {
        "type": "string",
        "description": "Person to greet",
    }
    assert schema["parameters"]["required"] == ["name"]


def test_optional_arguments_are_not_required():
    @registry.tool
    def roll_dice(sides: int = 6) -> str:
        """Roll a dice."""
        return str(sides)

    parameters = registry.get_schemas()[0]["function"]["parameters"]

    assert parameters["properties"]["sides"]["type"] == "integer"
    assert parameters["required"] == []


def test_list_tools_returns_name_and_description():
    @registry.tool
    def say_hello() -> str:
        """Say hello."""
        return "Hello!"

    assert registry.list_tools() == [
        {"name": "say_hello", "description": "Say hello."}
    ]


@pytest.mark.asyncio
async def test_run_tool_executes_registered_function():
    @registry.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    ok, result = await registry.run_tool("add", {"a": 2, "b": 3})

    assert ok is True
    assert result == "5"


@pytest.mark.asyncio
async def test_run_tool_ignores_unknown_arguments():
    @registry.tool
    def greet(name: str) -> str:
        """Greet a person."""
        return f"Hello, {name}!"

    ok, result = await registry.run_tool(
        "greet",
        {"name": "Raj", "unexpected": "ignored"},
    )

    assert ok is True
    assert result == "Hello, Raj!"


@pytest.mark.asyncio
async def test_run_tool_handles_unknown_tool():
    ok, result = await registry.run_tool("missing_tool", {})

    assert ok is False
    assert result == "Unknown tool 'missing_tool'"


@pytest.mark.asyncio
async def test_run_tool_returns_error_when_function_fails():
    @registry.tool
    def broken_tool() -> str:
        """A tool that raises an error."""
        raise ValueError("test failure")

    ok, result = await registry.run_tool("broken_tool", {})

    assert ok is False
    assert result == "broken_tool failed: test failure"


@pytest.mark.asyncio
async def test_run_tool_supports_async_functions():
    @registry.tool
    async def async_greet(name: str) -> str:
        """Greet asynchronously."""
        return f"Hello, {name}!"

    ok, result = await registry.run_tool("async_greet", {"name": "Raj"})

    assert ok is True
    assert result == "Hello, Raj!"
