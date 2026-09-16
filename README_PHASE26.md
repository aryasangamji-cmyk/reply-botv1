PHASE 26 - Combo Message Builder Correctly Packaged

IMPORTANT: app/bot.py is at the ZIP ROOT so unzip -o from /opt/new-ai-bot replaces the running bot.

Combo workflow:
1. Admin -> Combos -> Add Combo
2. Enter name, price, details
3. Tap Add Message
4. Choose Text or Photo
5. Text: send exact message -> Save Message -> Add Message again
6. Photo: send photo 1 -> send another photo or Skip/Finish Photos -> caption -> exact visible link text or Skip -> URL -> Save Message -> Add Message again
7. Repeat for any number of messages; order is preserved.
8. Combo sends each saved message in exact order.

Photos are copied into data/media/combos and stored as absolute project paths in the new DB record.
Telegram formatting entities and embedded text links are preserved.
