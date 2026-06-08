interface Props {
  trialEndsAt: string;
  /** No-upfront-payment: when the operator has no card on file yet, the banner
   *  nudges them to add one before the trial ends. */
  hasPaymentMethod: boolean;
}

function daysRemaining(isoDate: string): number {
  const end = new Date(isoDate).getTime();
  const now = Date.now();
  return Math.max(0, Math.ceil((end - now) / (1000 * 60 * 60 * 24)));
}

export function TrialBanner({ trialEndsAt, hasPaymentMethod }: Props) {
  const days = daysRemaining(trialEndsAt);
  const endDate = new Date(trialEndsAt).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
  });

  const daysLabel =
    days === 0
      ? "Trial ends today"
      : `${days} day${days === 1 ? "" : "s"} left in your trial`;

  // No card on file → amber, action-oriented: add a card before the trial ends.
  if (!hasPaymentMethod) {
    return (
      <div className="border-b border-amber-200 bg-amber-50 px-4 py-2.5">
        <div className="mx-auto max-w-4xl text-center">
          <p className="text-sm text-amber-800">
            <span className="font-semibold">{daysLabel}</span>
            {" — add a card before "}
            {endDate} to keep reports flowing{" "}
            <a
              href="/accounts/account"
              className="font-medium underline underline-offset-2 hover:text-amber-900"
            >
              →
            </a>
          </p>
        </div>
      </div>
    );
  }

  // Card on file → informational sky banner (unchanged copy).
  return (
    <div className="border-b border-sky-200 bg-sky-50 px-4 py-2.5">
      <div className="mx-auto max-w-4xl text-center">
        <p className="text-sm text-sky-800">
          <span className="font-semibold">{daysLabel}</span>
          {" — "}
          we'll charge based on your final array count on {endDate}. Add clients
          now so your first bill reflects the right amount.
        </p>
      </div>
    </div>
  );
}
