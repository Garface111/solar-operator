import { useEffect, useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { ClientCard } from "./ClientCard";
import { AddClientModal } from "./AddClientModal";
import { type ClientRow, listClients } from "../lib/api";

interface Props {
  /** Client id to auto-expand on load (from a /clients/:id deep link). */
  expandClientId?: number;
}

export function ClientsSection({ expandClientId }: Props) {
  const toast = useToast();
  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listClients()
      .then((rows) => {
        if (!cancelled) setClients(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          toast.error(err instanceof Error ? err.message : "Couldn't load clients");
          setClients([]);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function replaceClient(updated: ClientRow) {
    setClients((cs) => (cs ? cs.map((c) => (c.id === updated.id ? updated : c)) : cs));
  }

  function addClientLocal(c: ClientRow) {
    setClients((cs) => (cs ? [...cs, c].sort((a, b) => a.name.localeCompare(b.name)) : [c]));
  }

  return (
    <section>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
          Clients
          {clients && (
            <span className="ml-2 text-sm font-normal text-zinc-400">
              {clients.length}
            </span>
          )}
        </h2>
        <Button onClick={() => setAdding(true)} className="px-4 py-2">
          + Add client
        </Button>
      </div>

      {clients === null ? (
        <Card>
          <div className="flex items-center gap-2 text-sm text-zinc-400">
            <Spinner className="h-4 w-4" />
            Loading clients…
          </div>
        </Card>
      ) : clients.length === 0 ? (
        <Card>
          <p className="text-center text-sm text-zinc-500">
            No clients yet. Add your first reporting client to get started.
          </p>
        </Card>
      ) : (
        <div className="space-y-3">
          {clients.map((c) => (
            <ClientCard
              key={c.id}
              client={c}
              defaultExpanded={c.id === expandClientId}
              onChange={replaceClient}
            />
          ))}
        </div>
      )}

      <AddClientModal
        open={adding}
        onClose={() => setAdding(false)}
        onCreated={addClientLocal}
      />
    </section>
  );
}
