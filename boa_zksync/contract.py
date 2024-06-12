import textwrap
from contextlib import contextmanager
from typing import Optional

from boa.contracts.abi.abi_contract import ABIContract, ABIFunction
from boa.contracts.vyper.vyper_contract import VyperContract
from boa.rpc import to_bytes, to_int
from boa.util.abi import Address
from cached_property import cached_property
from vyper.semantics.analysis.base import VarInfo
from vyper.semantics.types import HashMapT
from vyper.semantics.types.function import ContractFunctionT

from boa_zksync.compile import compile_zksync_source
from boa_zksync.compiler_utils import (
    detect_expr_type,
    generate_source_for_arbitrary_stmt,
    generate_source_for_internal_fn,
)
from boa_zksync.types import ZksyncCompilerData


class ZksyncContract(ABIContract):
    """
    A contract deployed to the Zksync network.
    """

    def __init__(self, compiler_data: ZksyncCompilerData, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compiler_data = compiler_data

    def eval(self, code):
        return ZksyncEval(code, self)()

    @contextmanager
    def override_vyper_namespace(self):
        with self.vyper_contract.override_vyper_namespace():
            yield

    @cached_property
    def vyper_contract(self):
        return VyperContract(
            self.compiler_data.vyper,
            env=self.env,
            override_address=self.address,
            skip_initcode=True,
            filename=self.filename,
        )

    @cached_property
    def _storage(self):
        def storage():
            return None

        for name, var in self.compiler_data.global_ctx.variables.items():
            if not var.is_immutable and not var.is_constant:
                setattr(storage, name, ZksyncInternalVariable(var, name, self))
        return storage

    @cached_property
    def internal(self):
        def internal():
            return None

        for fn in self.compiler_data.global_ctx.functions:
            typ = fn._metadata["type"]
            if typ.is_internal:
                setattr(internal, fn.name, ZksyncInternalFunction(typ, self))
        return internal

    def get_logs(self):
        receipt = self.env.last_receipt
        if not receipt:
            raise ValueError("No logs available")

        receipt_source = Address(receipt["contractAddress"] or receipt["to"])
        if receipt_source != self.address:
            raise ValueError(
                f"Logs are no longer available for {self}, "
                f"the last called contract was {receipt_source}"
            )

        c = self.vyper_contract
        ret = []
        for log in receipt["logs"]:
            address = Address(log["address"])
            if address != self.address:
                continue
            index = to_int(log["logIndex"])
            topics = [to_int(topic) for topic in log["topics"]]
            data = to_bytes(log["data"])
            event = (index, address.canonical_address, topics, data)
            ret.append(c.decode_log(event))
        return ret


class _ZksyncInternal(ABIFunction):
    """
    An ABI function that temporarily changes the bytecode at the contract's address.
    """

    @cached_property
    def _override_bytecode(self) -> bytes:
        data = self.contract.compiler_data
        source = "\n".join((data.source_code, self.source_code))
        compiled = compile_zksync_source(source, self.name, data.compiler_args)
        return compiled.bytecode

    @property
    def source_code(self):
        raise NotImplementedError  # to be implemented in subclasses

    def __call__(self, *args, **kwargs):
        env = self.contract.env
        balance_before = env.get_balance(env.eoa)
        env.set_code(self.contract.address, self._override_bytecode)
        env.set_balance(env.eoa, 10**20)
        try:
            return super().__call__(*args, **kwargs)
        finally:
            env.set_balance(env.eoa, balance_before)
            env.set_code(self.contract.address, self.contract.compiler_data.bytecode)


class ZksyncInternalFunction(_ZksyncInternal):
    def __init__(self, fn: ContractFunctionT, contract: ZksyncContract):
        abi = {
            "anonymous": False,
            "inputs": [
                {"name": arg.name, "type": arg.typ.abi_type.selector_name()}
                for arg in fn.arguments
            ],
            "outputs": (
                [{"name": fn.name, "type": fn.return_type.abi_type.selector_name()}]
                if fn.return_type
                else []
            ),
            "stateMutability": fn.mutability.value,
            "name": f"__boa_private_{fn.name}__",
            "type": "function",
        }
        super().__init__(abi, contract._name)
        self.contract = contract
        self.func_t = fn

    @cached_property
    def source_code(self):
        return generate_source_for_internal_fn(self)


class ZksyncInternalVariable(_ZksyncInternal):
    def __init__(self, var: VarInfo, name: str, contract: ZksyncContract):
        if isinstance(var.typ, HashMapT):
            inputs, output = var.typ.getter_signature
        else:
            inputs, output = [], var.typ
        abi = {
            "anonymous": False,
            "inputs": [
                {"name": f"arg{index}", "type": arg.abi_type.selector_name()}
                for index, arg in enumerate(inputs)
            ],
            "outputs": [{"name": name, "type": output.abi_type.selector_name()}],
            "name": f"__boa_private_{name}__",
            "constant": True,
            "type": "function",
        }
        super().__init__(abi, contract._name)
        self.contract = contract
        self.var = var
        self.var_name = name

    def get(self, *args):
        return self.__call__(*args)

    @cached_property
    def source_code(self):
        inputs, output_type = self._abi["inputs"], self.return_type[0]
        getter_call = "".join(f"[{i['name']}]" for i in inputs)
        args_signature = ", ".join(f"{i['name']}: {i['type']}" for i in inputs)
        return textwrap.dedent(
            f"""
            @external
            @payable
            def __boa_private_{self.var_name}__({args_signature}) -> {output_type}:
                return self.{self.var_name}{getter_call}
        """
        )


class ZksyncEval(_ZksyncInternal):
    def __init__(self, code: str, contract: ZksyncContract):
        typ = detect_expr_type(code, contract)
        abi = {
            "anonymous": False,
            "inputs": [],
            "outputs": (
                [{"name": "eval", "type": typ.abi_type.selector_name()}] if typ else []
            ),
            "name": "__boa_debug__",
            "type": "function",
        }
        super().__init__(abi, contract._name)
        self.contract = contract
        self.code = code

    @cached_property
    def source_code(self):
        return generate_source_for_arbitrary_stmt(self.code, self.contract)
