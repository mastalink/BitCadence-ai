export function App() {
  return (
    <main className="shell">
      <h1>BitCadence Desktop</h1>
      <section className="status" role="status">
        <h2><span className="indicator" aria-hidden="true" />Runtime offline</h2>
        <p>The desktop isn’t connected to the runtime.</p>
      </section>
    </main>
  );
}
