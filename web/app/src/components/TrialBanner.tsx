interface Props {
  trialEndsAt: string;
}

function daysRemaining(isoDate: string): number {
  const end = new Date(isoDate).getTime();
  const now = Date.now();
  return Math.max(0, Math.ceil((end - now) / (1000 * 60 * 60 * 24)));
}

export function TrialBanner({ trialEndsAt }: Props) {
  const days = daysRemaining(trialEndsAt);
  const endDate = new Date(trialEndsAt).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
  });

  return (
    <div className="border-b border-sky-200 bg-sky-50 px-4 py-2.5">
      <div className="mx-auto max-w-4xl">
        <p className="text-sm text-sky-800">
          <span className="font-semibold">
            {days === 0 ? "Trial ends today" : `${days} day${days === 1 ? "" : "s"} left in your trial`}
          </span>
          {" — "}
          we'll charge based on your final array count on {endDate}. Add clients
          now so your first bill reflects the right amount.
        </p>
      </div>
    </div>
  );
}
