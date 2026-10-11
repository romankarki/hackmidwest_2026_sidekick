# Sidekick: 3-minute demo video script

**Runtime:** about 3:00. **Voiceover:** 347 words, about 2:20 at a natural pace of 150 words per minute. That leaves about 40 seconds of silence for typing, Sidekick's live replies and clicking through.

In the table, **bold** = what you say, `code` = what you type, *italics* = what's on screen.

## Before you hit record
- **Pre-warm one preview.** A real PR preview takes 3–4 minutes to build, so the video cuts. Either run the feature request once before recording and keep its PR and preview open in tabs, or record live and cut at ✂️.
- **Tabs, in order:**
  1. Slack: an empty new channel `#shoplite-team`
  2. Sidekick dashboard → **Integrations**, then the **Model** page
  3. CloudWatch dashboard **ShopLite-on-Akamai**
  4. DevRev → Tickets, plus Agent Studio (Customer Signals Hub)
  5. ShopLite prod: https://shop.172-232-189-143.sslip.io
- **Brain mode:** **LLM agent**, model **Kimi K3 on Amazon Bedrock**.
- **Slack:** zoom to 125%, hide the sidebar clutter, notifications off.

---

| Time | Screen | Voiceover |
|---|---|---|
| **0:00–0:15**<br>Hook | *Black screen, then a Slack message fades in:* "Northwind won't renew unless we add sort by price." | **Every team has more work than people. Sidekick is an AI teammate that lives in your chat: it answers like an SRE, builds like a developer, and keeps product and support in the loop.** |
| **0:15–0:35**<br>The team | *ShopLite storefront, then a simple architecture slide or the Sidekick README diagram* | **Meet ShopLite, a small online store. Like most teams, it runs on more than one cloud: the app and its PR previews run on Akamai Cloud, with Kubernetes; monitoring and AI models run on AWS; customer feedback lives in DevRev; code lives in GitHub. That's a lot of tabs. Sidekick is the one place to ask.** |
| **0:35–0:45**<br>Add Sidekick | *In `#shoplite-team`:* `/invite @sidekick` | **Adding it is one command. No setup per person: anyone on the team can use it.** |
| **0:45–1:15**<br>Who are you | `@sidekick who are you?`<br>*Reply shows the dashboard link, hosting, model and integrations. Click through to the dashboard → Integrations list → Model page (Bedrock, Kimi K3, "Test model ✓").* | **It tells you who it is and where it's configured. Here's its dashboard: AWS, Akamai, GitHub and DevRev are all connected, each with least-privilege keys. The brain is built with AWS Strands Agents, running a model on Amazon Bedrock, and every tool call streams live into Slack.** |
| **1:15–1:35**<br>Costs | `@sidekick what's running on our infra and what does it cost this month?` | **Ask what's running and what it costs, and it checks both clouds live: the Akamai VMs, the Kubernetes cluster and the load balancer, then AWS Cost Explorer and our budgets. Run rate, credits left, all in one answer, for the whole team.** |
| **1:35–1:55**<br>CloudWatch | `@sidekick is production healthy? check CloudWatch`<br>*Then cut to the CloudWatch dashboard graphs.* | **Production runs on Akamai, but its metrics stream into Amazon CloudWatch. Sidekick reads the latency, errors and alarms, so "is it healthy?" gets real numbers, not guesses.** |
| **1:55–2:25**<br>DevRev | `@sidekick what are customers asking for? use DevRev`<br>*Cut to DevRev: tickets from email and Slack, the standards article, then Agent Studio showing the Customer Signals Hub and its skills.* | **Customer context lives in DevRev. Support emails and our feedback channel flow in as tickets, next to our engineering standards doc. Sidekick consults DevRev's own Agent Studio agents, which aggregate the signals with their skills, and gets back ranked requests with ticket evidence: wishlist, a checkout bug, and Northwind's price sorting.** |
| **2:25–2:50**<br>Build it | `@sidekick add sort by price for Northwind, follow our coding standards, and raise a PR`<br>*Reply shows the diff and the cited § standards → click **Approve** → ✂️ cut → GitHub PR → the 🔍 preview comment → open the preview: the sort dropdown works → DevRev ticket shows Sidekick's comment.* | **Now let's build it. Sidekick checks our standards in DevRev, writes the change and shows the exact diff. Nothing ships without approval. One click opens a pull request, and every PR gets its own live preview on Akamai Kubernetes, while DevRev gets updated so the customer's account team knows.** |
| **2:50–3:00**<br>Close | *Slack thread overview, then the Sidekick logo* | **From a PM asking what customers want, to an engineer checking production: one teammate, every cloud, always with a human in the loop. That's Sidekick.** |

---

## Notes
- **If you're running long,** trim the costs beat (1:15) to its first sentence. The DevRev and build beats are the core story.
- **If a live answer is slow,** cut while it streams. The tool-call steps updating in Slack *are* good visuals, so show a second or two of them.
- **Safety beats worth showing on screen**, if time allows:
  - the "📝 Proposed, not applied yet" line;
  - the 5-preview limit and the kill switch on the dashboard's **Previews** page.
- **After the video:** close the demo PR (or merge it to show auto-deploy), and delete its preview from the dashboard **Previews** page.
