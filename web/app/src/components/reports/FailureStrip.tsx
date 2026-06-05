import { Link } from "react-router-dom";

export interface DeliveryFailure {
  clientName: string;
  reason: string | null;
  bouncedAt: string;
}

interface Props {
  failures: DeliveryFailure[];
}

/** Red strip shown at the top of the reports page when any delivery bounced. */
export function FailureStrip({ failures }: Props) {
  if (failures.length === 0) return null;

  return (
    <div
      role="alert"
      className="rounded-xl border border-red-200 bg-red-50 px-5 py-4"
    >
      <p className="text-sm font-semibold text-red-700">
        {failures.length === 1
          ? "1 delivery failed"
          : `${failures.length} deliveries failed`}
      </p>
      <ul className="mt-2 space-y-1">
        {failures.map((f, i) => (
          <li key={i} className="text-sm text-red-600">
            <span className="font-medium">{f.clientName}</span>
            {f.reason && (
              <span className="ml-1 opacity-70">— {f.reason}</span>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-red-500">
        Fix contact emails in{" "}
        <Link to="/clients" className="underline underline-offset-2 hover:text-red-700">
          Clients
        </Link>
        , then re-send from the quarter row below.
      </p>
    </div>
  );
}
