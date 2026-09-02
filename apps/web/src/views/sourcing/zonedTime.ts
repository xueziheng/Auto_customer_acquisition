const zonedIsoPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-](\d{2}):(\d{2}))$/;

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return leap ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

export function parseZonedIsoInstant(value: string | null | undefined): number | null {
  if (!value) return null;
  const match = zonedIsoPattern.exec(value);
  if (!match) return null;
  const [, yearValue, monthValue, dayValue, hourValue, minuteValue, secondValue, zone, offsetHourValue, offsetMinuteValue] = match;
  const year = Number(yearValue);
  const month = Number(monthValue);
  const day = Number(dayValue);
  const hour = Number(hourValue);
  const minute = Number(minuteValue);
  const second = Number(secondValue);
  const offsetHour = Number(offsetHourValue ?? 0);
  const offsetMinute = Number(offsetMinuteValue ?? 0);
  if (
    month < 1 || month > 12
    || day < 1 || day > daysInMonth(year, month)
    || hour > 23 || minute > 59 || second > 59
    || (zone !== "Z" && (offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)))
  ) return null;
  const instant = Date.parse(value);
  return Number.isFinite(instant) ? instant : null;
}

export function displayZonedIsoTime(value: string | null | undefined): string {
  return parseZonedIsoInstant(value) === null
    ? "未知"
    : value!.replace("T", " ").replace(/Z$/, " UTC");
}

export function elapsedZonedSeconds(
  start: string | null | undefined,
  end: string | null | undefined,
): number | null {
  const startTime = parseZonedIsoInstant(start);
  const endTime = parseZonedIsoInstant(end);
  if (startTime === null || endTime === null || endTime < startTime) return null;
  return Math.floor((endTime - startTime) / 1000);
}
