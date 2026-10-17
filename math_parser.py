"""
math_parser.py — Local Deterministic Math & Unit Conversion Engine for J.A.R.V.I.S.

Provides:
- 100% offline, zero-latency arithmetic evaluator using Python's AST (strictly NO LLM, NO eval()).
- Safe evaluation of +, -, *, /, //, %, ^, **, sqrt, abs, sin, cos, tan, log, percentages.
- Percentage logic: "15% of 200" -> 30, "100 + 15%" -> 115, "100 - 20%" -> 80.
- Unit converter for length, mass, temperature, volume, and time.
- Natural speech query parsing:
    - "what is 25 times 4"
    - "calculate 15 percent of 250"
    - "convert 10 miles to kilometers"
    - "how many feet in 2 meters"
    - "convert 100 fahrenheit to celsius"
- Unit-testable interfaces with zero external or Qt dependencies.
"""

from __future__ import annotations

import ast
import math
import re
from typing import Any, Dict, Optional, Tuple, Union


# =====================================================================
# Unit Conversion Constants (Base units: meter, gram, liter, second, celsius)
# =====================================================================

# Length -> Base: meters (m)
LENGTH_TO_METERS: Dict[str, float] = {
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "nmi": 1852.0, "nautical mile": 1852.0, "nautical miles": 1852.0,
}

# Mass -> Base: grams (g)
MASS_TO_GRAMS: Dict[str, float] = {
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0, "kilo": 1000.0, "kilos": 1000.0,
    "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
    "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
    "t": 1000000.0, "ton": 1000000.0, "tons": 1000000.0, "tonne": 1000000.0, "tonnes": 1000000.0,
}

# Volume -> Base: liters (l)
VOLUME_TO_LITERS: Dict[str, float] = {
    "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001, "millilitre": 0.001, "millilitres": 0.001,
    "l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0,
    "fl oz": 0.0295735, "fluid ounce": 0.0295735, "fluid ounces": 0.0295735,
    "cup": 0.236588, "cups": 0.236588,
    "pt": 0.473176, "pint": 0.473176, "pints": 0.473176,
    "qt": 0.946353, "quart": 0.946353, "quarts": 0.946353,
    "gal": 3.78541, "gallon": 3.78541, "gallons": 3.78541,
}

# Time -> Base: seconds (s)
TIME_TO_SECONDS: Dict[str, float] = {
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "wk": 604800.0, "week": 604800.0, "weeks": 604800.0,
    "mo": 2592000.0, "month": 2592000.0, "months": 2592000.0,
    "yr": 31536000.0, "year": 31536000.0, "years": 31536000.0,
}

# Temperature unit names
TEMP_UNITS: Dict[str, str] = {
    "c": "celsius", "celsius": "celsius", "centigrade": "celsius", "°c": "celsius",
    "f": "fahrenheit", "fahrenheit": "fahrenheit", "°f": "fahrenheit",
    "k": "kelvin", "kelvin": "kelvin", "°k": "kelvin",
}


# =====================================================================
# Safe AST Arithmetic Evaluator
# =====================================================================

SAFE_FUNCTIONS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}

SAFE_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


class _SafeEvaluator(ast.NodeVisitor):
    def visit(self, node: ast.AST) -> Any:
        method = "visit_" + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"Unsupported syntax expression: {node.__class__.__name__}")

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value)}")

    def visit_Num(self, node: ast.Num) -> Any:  # Python <=3.7 compat
        return node.n

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id.lower() in SAFE_CONSTANTS:
            return SAFE_CONSTANTS[node.id.lower()]
        raise ValueError(f"Unknown variable or identifier: '{node.id}'")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        elif isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary operator: {node.op.__class__.__name__}")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        elif isinstance(node.op, ast.Sub):
            return left - right
        elif isinstance(node.op, ast.Mult):
            return left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
            return left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("Modulo by zero")
            return left % right
        elif isinstance(node.op, (ast.Pow, ast.BitXor)):  # '^' often used for power
            if abs(right) > 1000 or (abs(left) > 100 and right > 100):
                raise ValueError("Exponent too large")
            return left ** right
        raise ValueError(f"Unsupported binary operator: {node.op.__class__.__name__}")

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are permitted")
        func_name = node.func.id.lower()
        if func_name not in SAFE_FUNCTIONS:
            raise ValueError(f"Function '{func_name}' is not in safe function whitelist")
        args = [self.visit(arg) for arg in node.args]
        return SAFE_FUNCTIONS[func_name](*args)


def evaluate_expression(expr: str) -> Tuple[bool, Union[float, int, None], str]:
    """
    Safely evaluates a mathematical expression string using AST parsing.
    Returns (success: bool, result: float|int|None, error_or_formatted: str).
    """
    clean = expr.strip()
    if not clean:
        return False, None, "Empty expression"

    # Normalize caret to power
    clean = clean.replace("^", "**")

    # Handle percentage patterns
    # 1. "X% of Y" or "X percent of Y" -> (X / 100) * Y
    m_pct_of = re.match(r"^(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(.+)$", clean, re.IGNORECASE)
    if m_pct_of:
        pct_val = float(m_pct_of.group(1))
        base_expr = m_pct_of.group(2).strip()
        ok, base_res, _ = evaluate_expression(base_expr)
        if ok and base_res is not None:
            ans = round((pct_val / 100.0) * base_res, 10)
            return True, ans, _format_number(ans)

    # 2. "X + Y%" or "X - Y%"
    m_pct_add = re.match(r"^(.+?)\s*([+-])\s*(\d+(?:\.\d+)?)\s*(?:%|percent)$", clean, re.IGNORECASE)
    if m_pct_add:
        base_expr = m_pct_add.group(1).strip()
        op = m_pct_add.group(2)
        pct_val = float(m_pct_add.group(3))
        ok, base_res, _ = evaluate_expression(base_expr)
        if ok and base_res is not None:
            factor = (1.0 + pct_val / 100.0) if op == "+" else (1.0 - pct_val / 100.0)
            ans = round(base_res * factor, 10)
            return True, ans, _format_number(ans)

    try:
        tree = ast.parse(clean, mode="eval")
        evaluator = _SafeEvaluator()
        result = evaluator.visit(tree)
        if isinstance(result, (int, float)):
            if math.isinf(result) or math.isnan(result):
                return False, None, "Calculation resulted in an undefined value"
            return True, result, _format_number(result)
        return False, None, "Invalid result type"
    except ZeroDivisionError:
        return False, None, "Cannot divide by zero, sir."
    except Exception as exc:
        return False, None, str(exc)


def _format_number(val: Union[int, float]) -> str:
    """Formats numeric result nicely (e.g. 10.0 -> '10', 3.14159 -> '3.1416')."""
    if isinstance(val, int):
        return f"{val:,}"
    if val.is_integer():
        return f"{int(val):,}"
    # Round to reasonable precision (up to 4 decimal places)
    rounded = round(val, 4)
    if rounded.is_integer():
        return f"{int(rounded):,}"
    return f"{rounded:,}"


# =====================================================================
# Unit Conversion Engine
# =====================================================================

def convert_units(value: float, from_unit: str, to_unit: str) -> Tuple[bool, Optional[float], str]:
    """
    Converts a value between compatible units (length, mass, temperature, volume, time).
    Returns (success: bool, converted_value: Optional[float], formatted_message: str).
    """
    f_u = from_unit.strip().lower()
    t_u = to_unit.strip().lower()

    # 1. Temperature Conversion
    f_temp = TEMP_UNITS.get(f_u)
    t_temp = TEMP_UNITS.get(t_u)
    if f_temp and t_temp:
        # Convert to Celsius first
        if f_temp == "celsius":
            celsius = value
        elif f_temp == "fahrenheit":
            celsius = (value - 32.0) * (5.0 / 9.0)
        elif f_temp == "kelvin":
            celsius = value - 273.15
        else:
            return False, None, f"Unknown temperature unit: {from_unit}"

        # Convert from Celsius to target
        if t_temp == "celsius":
            ans = celsius
        elif t_temp == "fahrenheit":
            ans = (celsius * (9.0 / 5.0)) + 32.0
        elif t_temp == "kelvin":
            ans = celsius + 273.15
        else:
            return False, None, f"Unknown temperature unit: {to_unit}"

        formatted = f"{_format_number(ans)} degrees {t_temp.capitalize()}"
        return True, ans, formatted

    # 2. Length Conversion
    if f_u in LENGTH_TO_METERS and t_u in LENGTH_TO_METERS:
        in_meters = value * LENGTH_TO_METERS[f_u]
        ans = in_meters / LENGTH_TO_METERS[t_u]
        return True, ans, f"{_format_number(ans)} {t_u}"

    # 3. Mass Conversion
    if f_u in MASS_TO_GRAMS and t_u in MASS_TO_GRAMS:
        in_grams = value * MASS_TO_GRAMS[f_u]
        ans = in_grams / MASS_TO_GRAMS[t_u]
        return True, ans, f"{_format_number(ans)} {t_u}"

    # 4. Volume Conversion
    if f_u in VOLUME_TO_LITERS and t_u in VOLUME_TO_LITERS:
        in_liters = value * VOLUME_TO_LITERS[f_u]
        ans = in_liters / VOLUME_TO_LITERS[t_u]
        return True, ans, f"{_format_number(ans)} {t_u}"

    # 5. Time Conversion
    if f_u in TIME_TO_SECONDS and t_u in TIME_TO_SECONDS:
        in_seconds = value * TIME_TO_SECONDS[f_u]
        ans = in_seconds / TIME_TO_SECONDS[t_u]
        return True, ans, f"{_format_number(ans)} {t_u}"

    return False, None, f"Cannot convert between incompatible units: '{from_unit}' and '{to_unit}'"


# =====================================================================
# Natural Language Query Parser
# =====================================================================

_WORD_OPERATORS = [
    (r"\bmultiplied\s+by\b", "*"),
    (r"\btimes\b", "*"),
    (r"\bdivided\s+by\b", "/"),
    (r"\bover\b", "/"),
    (r"\bplus\b", "+"),
    (r"\bminus\b", "-"),
    (r"\bto\s+the\s+power\s+of\b", "^"),
    (r"\bsquared\b", "^ 2"),
    (r"\bcubed\b", "^ 3"),
    (r"\bmodulo\b", "%"),
    (r"\bmod\b", "%"),
]


def parse_math_or_unit_query(query: str) -> Optional[Tuple[str, str]]:
    """
    Detects if the query is a math calculation or unit conversion.
    Returns (raw_result_str, formatted_spoken_reply) if matched, or None.
    """
    clean = query.lower().strip(" ,.!?")

    # 1. Check Unit Conversion Queries:
    # "convert 10 miles to kilometers"
    # "how many feet in 2 meters"
    # "100 fahrenheit in celsius"
    m_conv = re.search(
        r"(?:convert\s+)?(\d+(?:\.\d+)?)\s+([a-zA-Z°]+(?:\s+[a-zA-Z]+)?)\s+(?:to|in|into)\s+([a-zA-Z°]+(?:\s+[a-zA-Z]+)?)$",
        clean,
    )
    if not m_conv:
        m_conv_how = re.search(
            r"how\s+many\s+([a-zA-Z°]+(?:\s+[a-zA-Z]+)?)\s+(?:in|are\s+in)\s+(\d+(?:\.\d+)?)\s+([a-zA-Z°]+(?:\s+[a-zA-Z]+)?)$",
            clean,
        )
        if m_conv_how:
            # Reorder groups: value, from_unit, to_unit
            val = float(m_conv_how.group(2))
            from_u = m_conv_how.group(3).strip()
            to_u = m_conv_how.group(1).strip()
            ok, res, msg = convert_units(val, from_u, to_u)
            if ok:
                return msg, f"{_format_number(val)} {from_u} is approximately {msg}, sir."

    if m_conv:
        val = float(m_conv.group(1))
        from_u = m_conv.group(2).strip()
        to_u = m_conv.group(3).strip()
        ok, res, msg = convert_units(val, from_u, to_u)
        if ok:
            return msg, f"{_format_number(val)} {from_u} is approximately {msg}, sir."

    # 2. Check Math Queries:
    # "what is 25 * 4", "calculate 15 percent of 200", "square root of 144"
    math_candidate = clean
    # Strip common leading question prefixes
    math_candidate = re.sub(
        r"^(?:what(?:'s|\s+is)?|calculate|solve|evaluate|compute|how\s+much\s+is)\s+",
        "",
        math_candidate,
    ).strip()

    # Square root spoken phrases: "square root of 144" -> "sqrt(144)"
    m_sqrt = re.match(r"(?:square\s+root\s+of|sqrt\s+of)\s+(.+)$", math_candidate)
    if m_sqrt:
        math_candidate = f"sqrt({m_sqrt.group(1).strip()})"

    # Spoken operator translation
    for pattern, op in _WORD_OPERATORS:
        math_candidate = re.sub(pattern, op, math_candidate)

    # Word numbers replacement
    from scheduler import _WORD_NUMBERS
    for word, num in sorted(_WORD_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
        if word in ("a", "an", "half", "quarter"):
            continue
        math_candidate = re.sub(rf"\b{re.escape(word)}\b", str(num), math_candidate)

    # Validate that math_candidate contains at least one digit or math function/operator
    has_math_tokens = bool(re.search(r"[\d+\-*/%^]|(?:sqrt|abs|sin|cos|tan|pi|e)\b", math_candidate))
    if not has_math_tokens:
        return None

    ok, res, msg = evaluate_expression(math_candidate)
    if ok and res is not None:
        return msg, f"The answer is {msg}, sir."

    return None
