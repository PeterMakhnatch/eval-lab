"""
Request validation utilities.
"""


def validate_roadmap_request(data):
    """
    Validates a roadmap generation request body.

    Args:
        data: The parsed JSON request body.

    Returns:
        A list of validation error strings, empty if valid.
    """
    errors = []

    if not isinstance(data, dict):
        errors.append("Request body must be a JSON object")
        return errors

    if 'feature' not in data or not isinstance(data['feature'], str) or len(data['feature'].strip()) == 0:
        errors.append("Field 'feature' is required and must be a non-empty string")

    if 'target_platforms' in data:
        if not isinstance(data['target_platforms'], list):
            errors.append("Field 'target_platforms' must be a list of strings")
        elif not all(isinstance(p, str) for p in data['target_platforms']):
            errors.append("All items in 'target_platforms' must be strings")

    if 'constraints' in data:
        constraints = data['constraints']
        if not isinstance(constraints, dict):
            errors.append("Field 'constraints' must be an object")
        else:
            if 'timeline_weeks' in constraints:
                tw = constraints['timeline_weeks']
                if not isinstance(tw, int) or tw < 1 or tw > 52:
                    errors.append("Field 'constraints.timeline_weeks' must be an integer between 1 and 52")
            if 'budget_usd' in constraints:
                b = constraints['budget_usd']
                if not isinstance(b, (int, float)) or b <= 0:
                    errors.append("Field 'constraints.budget_usd' must be a positive number")

    if 'preferred_technologies' in data:
        if not isinstance(data['preferred_technologies'], list):
            errors.append("Field 'preferred_technologies' must be a list of strings")
        elif not all(isinstance(t, str) for t in data['preferred_technologies']):
            errors.append("All items in 'preferred_technologies' must be strings")

    return errors
