import { useParams } from "react-router-dom";
import { ClientsSection } from "../components/ClientsSection";

export default function ClientsTab() {
  // Supports deep links to a specific client: /clients/:clientId auto-expands it.
  const { clientId } = useParams();

  return <ClientsSection expandClientId={clientId ? Number(clientId) : undefined} />;
}
