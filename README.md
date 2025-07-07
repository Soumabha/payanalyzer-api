# Pay Spend Analysis Dashboard

A web dashboard for analyzing and visualizing your expenses, with category and date filtering, monthly trends, top expenses, and export to Excel. The current supported integration is with Splitwise.

---

## Features
- Interactive dashboard for spend analysis
- Filter by date range and category
- Visualize monthly trends, category-wise spends, and top expenses
- View top recurring expenses (merged by similar description using stemming)
- Predict next 6 months' spend using ARIMA (or linear regression)
- Export all group expenses to Excel

---

## Installation

### 1. Clone the Repository
```bash
git clone <your-repo-url>
cd splitapi
```

### 2. Create a Virtual Environment (Recommended)
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python Dependencies
```bash
pip install -r requirements.txt
```

---

## Configuration

### 1. Splitwise API Credentials
Create a `.env` file in the project root with the following variables:

```
SPLITWISE_CONSUMER_KEY=your_splitwise_consumer_key
SPLITWISE_CONSUMER_SECRET=your_splitwise_consumer_secret
API_KEY=your_splitwise_api_key
SPLITWISE_GROUP_NAME=Your Group Name
```
- You can obtain these credentials by registering an app at https://dev.splitwise.com/apps
- `SPLITWISE_GROUP_NAME` should match the name of your Splitwise group exactly including casing.

#### For Export Script Only (Optional):
If you want to use the export script, add these to your `.env` as well:
```
ACCESS_TOKEN=your_splitwise_access_token
ACCESS_TOKEN_SECRET=your_splitwise_access_token_secret
GROUP_ID=your_splitwise_group_id
```

---

## Usage

### 1. Run the Dashboard
```bash
uvicorn main:app --reload
```
- The dashboard will be available at [http://localhost:8000](http://localhost:8000)
- The first run will sync your Splitwise group expenses to a local SQLite database (`splitwise_expenses.db`).

### 2. Using the Dashboard
- Use the filters at the top to select date range and category.
- View monthly trends, category-wise spends, top 10 expenses, top recurring expenses (with configurable limit), and predictions.

### 3. Export All Expenses to Excel
```bash
python splitwise_export.py
```
- This will create an Excel file with all expenses grouped by month for your group.
- Make sure your `.env` contains the export-specific variables.

---

## Project Structure

```
main.py                # FastAPI backend
splitwise_data.py      # Database and Splitwise sync logic
splitwise_export.py    # Export all expenses to Excel
requirements.txt       # Python dependencies
static/
  ├─ index.html        # Dashboard frontend
  ├─ dashboard.js      # Dashboard logic
  └─ style.css         # Dashboard styles
splitwise_expenses.db  # SQLite database (auto-created)
```

---

## Notes
- The `.env` file and `splitwise_expenses.db` are **not** tracked by git (see `.gitignore`).
- The dashboard only works for one Splitwise group at a time (set by `SPLITWISE_GROUP_NAME`).
- For large groups, the initial sync may take a few minutes.
- The Top Recurring Expenses section merges similar descriptions using stemming (NLTK's PorterStemmer). You may need to run `python -m nltk.downloader punkt` if you see NLTK errors.

---

## License
MIT 