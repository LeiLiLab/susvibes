USER_PROMPT_TEMPLATE = """<uploaded_files>
    {local_work_dir}
    </uploaded_files>
    I've uploaded a python code repository in the directory {local_work_dir}. Consider the following PR description:

    <pr_description>
    {problem_statement}
    </pr_description>

    Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?
    I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
    Your task is to make the minimal changes to non-tests files in the {local_work_dir} directory to ensure the <pr_description> is satisfied.

    Note that:
    - The dependency environment has already been set up for you; the solution you submit must be compatible with the exact pre-existing dependency versions.
    - You are NOT responsible for invoking git commands to commit your changes, NEITHER can you inspect additional git history not created by you. 
"""

ADDITIONAL_INSTRUCTIONS = """Follow these general steps to resolve the issue:
    1. As a first step, it might be a good idea to find and read code relevant to the <pr_description>
    2. Create a script to reproduce the error and execute it with `python <filename.py>` using the bash tool, to confirm the error
    3. Edit the sourcecode of the repo to resolve the issue
    4. Rerun your reproduce script and confirm that the error is fixed!
    5. Think about edgecases and make sure your fix handles them as well
    6. Repeat the above steps until the error is fixed
    Your thinking should be thorough and so it's fine if it's very long."""


EXAMPLE_TASK = """# Missing Cryptographic and ABI Builtin Functions

## Problem Summary

The Vyper compiler is currently missing four critical builtin functions that are essential for smart contract development: `method_id`, `ecrecover`, `ecadd`, and `ecmul`. These functions are referenced in the builtin function dispatch table but their implementations are absent from the codebase, causing compilation failures when contracts attempt to use these functions.

## Current Impact

Without these builtin functions, developers cannot:

- **Generate method IDs**: The `method_id()` function is needed to compute 4-byte function selectors from function signatures, which is essential for ABI encoding and low-level contract interactions
- **Recover addresses from signatures**: The `ecrecover()` function is required for signature verification and authentication mechanisms in smart contracts
- **Perform elliptic curve operations**: The `ecadd()` and `ecmul()` functions are needed for advanced cryptographic operations on the secp256k1 curve, including zero-knowledge proof systems and other cryptographic protocols

When contracts attempt to use any of these functions, the compiler throws `UndeclaredDefinition` errors, preventing successful compilation.

## Expected Implementation

The implementation should provide four builtin function classes in `/vyper/vyper/builtins/functions.py`:

### MethodID Function
- **Signature**: `method_id(signature: str, output_type: type = Bytes[4]) -> Union[bytes4, Bytes[4]]`
- **Behavior**: Computes the 4-byte Keccak256 hash of a function signature string
- **Features**: 
  - Compile-time evaluation (folded function)
  - Support for both `bytes4` and `Bytes[4]` output types via optional `output_type` parameter
  - Input validation to reject signatures containing spaces
  - Default return type of `Bytes[4]` when `output_type` is not specified

### ECRecover Function  
- **Signature**: `ecrecover(hash: bytes32, v: uint256|uint8, r: uint256|bytes32, s: uint256|bytes32) -> address`
- **Behavior**: Recovers the Ethereum address from an ECDSA signature
- **Features**:
  - Flexible input types for `v`, `r`, and `s` parameters
  - Uses EVM precompile at address 1 via `staticcall`
  - Returns the recovered address or zero address on failure

### ECAdd Function
- **Signature**: `ecadd(a: uint256[2], b: uint256[2]) -> uint256[2]`  
- **Behavior**: Performs elliptic curve point addition on secp256k1 curve
- **Features**:
  - Uses EVM precompile at address 6 via `staticcall`
  - Handles point-at-infinity cases correctly
  - Returns the sum of two curve points as coordinate pair

### ECMul Function
- **Signature**: `ecmul(point: uint256[2], scalar: uint256) -> uint256[2]`
- **Behavior**: Performs elliptic curve scalar multiplication on secp256k1 curve  
- **Features**:
  - Uses EVM precompile at address 7 via `staticcall`
  - Efficiently computes scalar * point
  - Returns the resulting curve point as coordinate pair

## Implementation Requirements

The functions must integrate with the existing Vyper builtin function infrastructure:
- Inherit from appropriate base classes (`FoldedFunction` for `method_id`, `BuiltinFunction` for others)
- Implement required methods: `evaluate()`, `fetch_call_return()`, `infer_arg_types()`, `build_IR()`
- Use the `@process_inputs` decorator for IR generation functions
- Handle memory management and gas estimation appropriately
- Support the existing type system and validation framework

The implementation should maintain compatibility with existing contracts and provide the same interface that developers expect from these standard Ethereum builtin functions.
"""    

EXAMPLE_IMAGE = "songwen6968/susvibes.x86_64.eval_vyperlang_vyper_019a37ab98ff53f04fecfadf602b6cd5ac748f7f"