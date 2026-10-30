// The notice after a sign-in (the public site): an early beta, what the lines do not account for, and
// how to report problems (GitHub issues, the site's e-mail, Report a problem). web.py's sign-in leaves
// the cookie WELCOME for a few minutes; the page shows the notice once and deletes it. A native
// <dialog> (showModal: Esc, the focus kept in it, the page behind inert).

import { useEffect, useRef, useState } from "preact/hooks";
import { Button } from "../ui/components";
import { Icon, Logo } from "../ui/icons";
import { canReport, ISSUES_URL, openReport, siteContact } from "../report";
import "./notices.css";

const WELCOME = "__Host-findaline-welcome";  // web.WELCOME_COOKIE

function welcomed(): boolean {
  return document.cookie.split(";").some((c) => c.trim().startsWith(WELCOME + "="));
}

function forget() {
  document.cookie = `${WELCOME}=; Max-Age=0; Path=/; Secure; SameSite=Lax`;
}

/** The contact as a link: mailto: for an e-mail address, the address of a web page, else text. */
function Contact({contact}: {contact: string}) {
  if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(contact)) return <a href={`mailto:${contact}`}>{contact}</a>;
  if (/^https:\/\//.test(contact)) return <a href={contact} target="_blank" rel="noopener">{contact}</a>;
  return <>{contact}</>;
}

export function BetaNotice() {
  const ref = useRef<HTMLDialogElement>(null);
  const [shown] = useState(() => canReport() && welcomed());
  useEffect(() => {
    if (!shown) return;
    forget();
    ref.current?.showModal();
  }, []);
  if (!shown) return null;
  const contact = siteContact();
  const close = () => ref.current?.close();
  return <dialog ref={ref} class="ui u-dialog n-beta" aria-labelledby="n-beta-title">
    <div class="n-head"><Logo /><span class="u-badge">beta</span></div>
    <h2 class="u-h" id="n-beta-title">An early beta</h2>
    <p>Find-a-Line is still young: results, data sources and the interface change, and some of it will be wrong.
      Check every line on the spot before you rig it.</p>
    <p class="n-warn"><Icon name="warn" /><span><b>A break of the main is not part of the calculation.</b> The
      clearance under a line assumes the main holds; a fall onto the backup, which sags lower, is not checked.</span></p>
    <p>Please tell us what goes wrong or what you miss, above all:</p>
    <ul>
      <li>lines you expected at a place but did not get, or results you did not expect;</li>
      <li>a water surface at a wrong height (a lake or the sea too high or too low);</li>
      <li>bugs of the interface.</li>
    </ul>
    <p class="n-ways">
      <a href={ISSUES_URL} target="_blank" rel="noopener"><Icon name="link" />GitHub issues</a>
      {contact && <span><Icon name="info" />E-mail: <Contact contact={contact} /></span>}
      <button type="button" class="c-link" onClick={() => { close(); openReport(); }}>
        <Icon name="warn" />Report a problem here</button>
    </p>
    <p class="u-small">You find Report a problem later in the account menu, in a line's details and in a search's ⋯
      menu.</p>
    <div class="n-actions"><Button kind="primary" onClick={close}>Understood</Button></div>
  </dialog>;
}
