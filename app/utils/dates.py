from datetime import date, timedelta


def zero_fill_days(data: list[dict], days: int, date_key: str = "date") -> list[dict]:
    if days <= 0:
        return []

    numeric_keys: set[str] = set()
    rows_by_date: dict[str, dict] = {}

    for row in data:
        date_value = str(row.get(date_key, "")).strip()
        if not date_value:
            continue

        rows_by_date[date_value] = dict(row)

        for key, value in row.items():
            if key == date_key:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numeric_keys.add(key)

    start_day = date.today() - timedelta(days=days - 1)
    filled: list[dict] = []

    for offset in range(days):
        current_day = start_day + timedelta(days=offset)
        current_iso = current_day.isoformat()

        existing = rows_by_date.get(current_iso)
        if existing is None:
            entry = {date_key: current_iso}
            for key in numeric_keys:
                entry[key] = 0
            filled.append(entry)
            continue

        entry = {date_key: current_iso}
        for key in numeric_keys:
            value = existing.get(key, 0)
            if isinstance(value, bool):
                entry[key] = int(value)
            elif isinstance(value, (int, float)):
                entry[key] = value
            else:
                entry[key] = 0
        filled.append(entry)

    return filled
