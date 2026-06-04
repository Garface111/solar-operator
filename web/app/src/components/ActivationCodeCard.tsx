import { Card } from "../ui/Card";
import { CopyButton } from "./CopyButton";

interface Props {
  tenantKey: string | null;
}

/**
 * Shows the tenant activation code the customer pastes into the Chrome
 * extension's options page so captures route to their account.
 */
export function ActivationCodeCard({ tenantKey }: Props) {
  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        Extension activation code
      </h2>
      <p className="mt-1 text-sm text-zinc-500">
        Paste this into the Solar Operator Chrome extension&apos;s options page to
        connect it to your account.
      </p>
      <p className="mt-1 text-xs font-medium text-amber-700">
        Treat this like a password — anyone with this code can send data to your account.
      </p>

      {tenantKey ? (
        <div className="mt-4 flex items-center gap-2 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
          <code className="flex-1 select-all break-all font-mono text-sm text-zinc-800">
            {tenantKey}
          </code>
          <CopyButton value={tenantKey} label="Copy code" />
        </div>
      ) : (
        <p className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          No activation code on file. Email support@solaroperator.org and
          we&apos;ll sort it out.
        </p>
      )}

      <ol className="mt-4 space-y-1.5 text-xs text-zinc-500">
        <li>1. Open the Solar Operator extension and click &ldquo;Options&rdquo;.</li>
        <li>2. Paste the code above into the activation field and save.</li>
        <li>
          3. Log into Green Mountain Power as usual — captures now flow to your
          account automatically.
        </li>
      </ol>
    </Card>
  );
}
