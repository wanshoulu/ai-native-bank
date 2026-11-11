# Contributing Guide

<!-- AUTO-GENERATED: Generated from requirements.txt, test files, start scripts, and server.py on 2026-06-04 -->

## Development Environment Setup

### Prerequisites

- Python 3.10 or higher
- OpenAI API key (or compatible API service key)
- (Optional) spaCy Chinese model: download `zh_core_web_sm` to `./data/`
- (Optional) spaCy English model: download `en_core_web_sm` to `./data/`

### Installation

1. Clone the repository
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # Linux/Mac
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Create `.env` file (see `.env.example`):
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

## Available Commands

| Command | Description |
|---------|-------------|
| `python server.py` | Start the server directly |
| `start.bat` | Windows wrapper to start the server |
| `./start.sh` | Linux wrapper to start the server in background |
| `uvicorn server:app --reload --host 0.0.0.0 --port 8000` | Development server with hot reload |
| `uvicorn server:app --host 0.0.0.0 --port 8000` | Production server (single worker) |
| `python -m pytest test/ -v` | Run all tests |
| `python -m pytest test/testcase.py -v` | Run API endpoint tests |
| `python -m pytest test/test_database.py -v` | Run database tests |
| `python -m pytest test/test_guardrails.py -v` | Run GuardRails tests |
| `python -m pytest test/test_pii.py -v` | Run PII detection tests |
| `python -m pytest test/test_checkpointer.py -v` | Run checkpointer tests |
| `python -m pytest test/test_agent_checkpointer.py -v` | Run agent checkpointer tests |
| `python -m pytest test/test_checkpoint_cleanup.py -v` | Run checkpoint cleanup tests |
| `pytest --cov=src --cov-report=term-missing` | Run tests with coverage |
| `black .` | Format code with Black |
| `flake8` | Lint code with Flake8 |
| `mypy .` | Type check with MyPy |

## Testing

### Running Tests

```bash
# All tests
python -m pytest test/ -v

# Specific test file
python -m pytest test/testcase.py -v
python -m pytest test/test_database.py -v
python -m pytest test/test_guardrails.py -v
python -m pytest test/test_pii.py -v
python -m pytest test/test_checkpointer.py -v
python -m pytest test/test_agent_checkpointer.py -v
python -m pytest test/test_checkpoint_cleanup.py -v

# With coverage
pytest --cov=src --cov-report=term-missing
```

### Test Structure

| File | Description |
|------|-------------|
| `test/testcase.py` | Unit and integration tests for API endpoints and agent functionality |
| `test/test_database.py` | Database CRUD operation tests |
| `test/test_guardrails.py` | GuardRails security rule tests |
| `test/test_pii.py` | PII detection tests |
| `test/test_checkpointer.py` | SQLite checkpointer persistence tests |
| `test/test_agent_checkpointer.py` | Agent-level checkpointer integration tests |
| `test/test_checkpoint_cleanup.py` | Checkpoint cleanup/gc tests |

### Writing New Tests

Follow the Arrange-Act-Assert pattern:

```python
def test_feature_description(self):
    # Arrange
    input_data = {"message": "test", "conversation_id": "test-123"}

    # Act
    response = self.client.post("/chat", json=input_data)

    # Assert
    assert response.status_code == 200
    assert "response" in response.json()
```

## Code Style

- **Formatter**: Black (line length 88)
- **Linter**: Flake8
- **Type Checker**: MyPy
- Follow PEP 8 conventions
- Use type annotations on all function signatures
- Keep functions under 50 lines
- Keep files under 800 lines
- Use immutable patterns where possible

## Pull Request Checklist

Before submitting a PR:

- [ ] All tests pass (`pytest test/ -v`)
- [ ] Code is formatted with Black
- [ ] No Flake8 linting errors
- [ ] Type checking passes with MyPy
- [ ] New functionality has test coverage
- [ ] No hardcoded secrets or credentials
- [ ] No `print()` statements (use `logging` module)
- [ ] Commit messages follow conventional format: `type: description`

<!-- END AUTO-GENERATED -->
