export interface FieldErrorProps {
  readonly id: string;
  readonly messages: readonly string[];
}

export function FieldError({
  id,
  messages,
}: FieldErrorProps): React.JSX.Element | null {
  if (messages.length === 0) return null;
  return (
    <div id={id} className="schema-form__field-error">
      {messages.map((message, index) => (
        <p key={`${message}-${String(index)}`}>{message}</p>
      ))}
    </div>
  );
}
