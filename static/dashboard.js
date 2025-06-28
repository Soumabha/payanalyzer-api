document.addEventListener("DOMContentLoaded", function() {
    // Section 1: Cumulative Stats
    fetch("/api/stats").then(r => r.json()).then(data => {
        document.getElementById("stats").innerHTML = `
            <p><strong>Total Expenses:</strong> ${data.total_expenses}</p>
            <p><strong>Total Spent:</strong> ₹${data.total_spent.toLocaleString('en-IN', {minimumFractionDigits: 2})}</p>
            <p><strong>Average Monthly Expense:</strong> ₹${data.average_expense.toLocaleString('en-IN', {minimumFractionDigits: 2})}</p>
            <p><strong>Date Range:</strong> ${data.date_range.start} to ${data.date_range.end}</p>
        `;
        if (data.group_name) {
            document.getElementById('groupName').textContent = data.group_name;
        }
    });

    // Sticky header quick stats
    fetch("/api/stats").then(r => r.json()).then(data => {
        const quickStats = document.getElementById("quickStats");
        if (quickStats) {
            quickStats.innerHTML = `
                <div class="stat-card"><span>Total</span><br>₹${data.total_spent.toLocaleString('en-IN', {minimumFractionDigits: 2})}</div>
                <div class="stat-card"><span>Avg/Month</span><br>₹${data.average_expense.toLocaleString('en-IN', {minimumFractionDigits: 2})}</div>
                <div class="stat-card"><span>Range</span><br>${data.date_range.start} to ${data.date_range.end}</div>
                <div class="stat-card"><span>Expenses</span><br>${data.total_expenses}</div>
            `;
        }
        if (data.group_name) {
            const groupName = document.getElementById('groupName');
            if (groupName) groupName.textContent = data.group_name;
        }
    });

    // Section 2: Monthwise Bar Chart
    let allMonths = [];
    let monthsData = [];

    // Fetch all months for dropdowns and initialize dashboard
    fetch("/api/monthly").then(r => r.json()).then(months => {
        allMonths = months.map(m => m.month);
        monthsData = months;
        const startDropdown = document.getElementById('monthStartDropdown');
        const endDropdown = document.getElementById('monthEndDropdown');
        if (!startDropdown || !endDropdown) return;
        startDropdown.innerHTML = '';
        endDropdown.innerHTML = '';
        months.forEach(m => {
            const opt1 = document.createElement('option');
            opt1.value = m.month;
            opt1.textContent = m.month;
            startDropdown.appendChild(opt1);
            const opt2 = document.createElement('option');
            opt2.value = m.month;
            opt2.textContent = m.month;
            endDropdown.appendChild(opt2);
        });
        // Default: start from 2021-07 if available, else earliest; end is latest
        let defaultStart = months.find(m => m.month === '2021-07') ? '2021-07' : months[0].month;
        let defaultEnd = months[months.length-1].month;
        startDropdown.value = defaultStart;
        endDropdown.value = defaultEnd;
        updateDashboardForRange(startDropdown.value, endDropdown.value);
        startDropdown.addEventListener('change', () => {
            let start = startDropdown.value;
            let end = endDropdown.value;
            if (allMonths.indexOf(start) > allMonths.indexOf(end)) {
                endDropdown.value = start;
                end = start;
            }
            updateDashboardForRange(start, end);
        });
        endDropdown.addEventListener('change', () => {
            let start = startDropdown.value;
            let end = endDropdown.value;
            if (allMonths.indexOf(start) > allMonths.indexOf(end)) {
                startDropdown.value = end;
                start = end;
            }
            updateDashboardForRange(start, end);
        });
    });

    function updateDashboardForRange(start, end) {
        updateCategoryCumulativeChart(start, end);
        updateMonthwiseBarChart(start, end);
        loadMonthRangeDetail(start, end);
    }

    function updateMonthwiseBarChart(start, end) {
        const startIdx = allMonths.indexOf(start);
        const endIdx = allMonths.indexOf(end);
        const rangeMonths = monthsData.slice(startIdx, endIdx + 1);
        const ctx = document.getElementById('monthlyBarChart')?.getContext('2d');
        if (!ctx) return;
        if (window.monthlyBarChartInstance && typeof window.monthlyBarChartInstance.destroy === 'function') {
            window.monthlyBarChartInstance.destroy();
        }
        window.monthlyBarChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: rangeMonths.map(m => m.month),
                datasets: [{
                    label: 'Total Spend (INR)',
                    data: rangeMonths.map(m => m.total),
                    backgroundColor: 'rgba(54, 162, 235, 0.5)'
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    y: {
                        ticks: {
                            callback: function(value) { return '₹' + value; }
                        }
                    }
                }
            }
        });
    }

    // Render the full monthly trend chart once on page load
    fetch("/api/monthly-trend").then(r => r.json()).then(trend => {
        const ctx = document.getElementById('monthlyTrendChart')?.getContext('2d');
        if (!ctx) return;
        if (window.monthlyTrendChartInstance && typeof window.monthlyTrendChartInstance.destroy === 'function') {
            window.monthlyTrendChartInstance.destroy();
        }
        window.monthlyTrendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: trend.map(m => m.month),
                datasets: [{
                    label: 'Monthly Spend (INR)',
                    data: trend.map(m => m.total),
                    borderColor: '#2c3e50',
                    backgroundColor: 'rgba(44,62,80,0.1)',
                    fill: true,
                    tension: 0.3
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: true },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return `₹${context.parsed.y.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        ticks: {
                            callback: function(value) { return '₹' + value; }
                        }
                    }
                }
            }
        });
    });

    // Category pie chart for selected range
    function renderCategoryPieChart(categories) {
        const ctx = document.getElementById('categoryPieChart')?.getContext('2d');
        if (!ctx) return;
        if (window.categoryPieChartInstance && typeof window.categoryPieChartInstance.destroy === 'function') {
            window.categoryPieChartInstance.destroy();
        }
        window.categoryPieChartInstance = new Chart(ctx, {
            type: 'pie',
            data: {
                labels: categories.map(c => c.category),
                datasets: [{
                    data: categories.map(c => c.total),
                    backgroundColor: categories.map((_, i) => `hsl(${i * 37 % 360}, 70%, 60%)`)
                }]
            },
            options: {
                plugins: {
                    legend: { display: true },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return `${context.label}: ₹${context.parsed.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                            }
                        }
                    }
                }
            }
        });
    }

    // Top N expenses table for selected range (now top 10)
    function renderTopExpensesTable(start, end) {
        fetch(`/api/top-expenses/${start}/${end}?limit=10`).then(r => r.json()).then(data => {
            let html = '<h3>Top 10 Expenses</h3>';
            html += '<table><thead><tr><th>Date</th><th>Description</th><th>Cost</th><th>Category</th></tr></thead><tbody>';
            data.forEach(e => {
                html += `<tr><td>${e.date}</td><td>${e.description}</td><td>₹${e.cost.toLocaleString('en-IN', {minimumFractionDigits: 2})}</td><td>${getCategoryIconHtml(e.category)}${e.category}</td></tr>`;
            });
            html += '</tbody></table>';
            const el = document.getElementById('topExpenses');
            if (el) el.innerHTML = html;
        });
    }

    // Patch the loadMonthRangeDetail function to use new charts/tables
    function loadMonthRangeDetail(start, end) {
        fetch(`/api/monthly-range/${start}/${end}`).then(r => r.json()).then(data => {
            const ctx = document.getElementById('categoryBarChart')?.getContext('2d');
            if (!ctx) return;
            if (window.categoryBarChartInstance && typeof window.categoryBarChartInstance.destroy === 'function') {
                window.categoryBarChartInstance.destroy();
            }
            window.categoryBarChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.categories.map(c => c.category),
                    datasets: [{
                        label: 'Spend by Category',
                        data: data.categories.map(c => c.total),
                        backgroundColor: 'rgba(255, 99, 132, 0.5)'
                    }]
                },
                options: {
                    responsive: true,
                    plugins: { legend: { display: false } }
                }
            });
            renderCategoryPieChart(data.categories);
            renderTopExpensesTable(start, end);
        });
    }

    // Section 3: Prediction
    fetch("/api/predict").then(r => r.json()).then(data => {
        const ctx = document.getElementById('predictionChart').getContext('2d');
        new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.prediction.map(p => p.month),
                datasets: [{
                    label: 'Predicted Spend',
                    data: data.prediction.map(p => p.predicted),
                    borderColor: 'rgba(75, 192, 192, 1)',
                    backgroundColor: 'rgba(75, 192, 192, 0.2)',
                    fill: true
                }]
            },
            options: {
                responsive: true
            }
        });
    });

    // Category icon mapping (Font Awesome or similar)
    const categoryIcons = {
        'Food & Drink': 'fa-utensils',
        'Shopping': 'fa-shopping-cart',
        'Home': 'fa-home',
        'Transportation': 'fa-car',
        'Utilities': 'fa-bolt',
        'Entertainment': 'fa-film',
        'Travel': 'fa-plane',
        'Personal': 'fa-user',
        'Groceries': 'fa-apple-alt',
        'Health': 'fa-heartbeat',
        'Other': 'fa-ellipsis-h',
        'Uncategorized': 'fa-question',
        '': 'fa-question',
        'General': 'fa-receipt',
        'Rent': 'fa-building',
        'Gifts': 'fa-gift',
        'Education': 'fa-graduation-cap',
        'Pets': 'fa-paw',
        'Car': 'fa-car',
        'Taxi': 'fa-taxi',
        'Parking': 'fa-parking',
        'Fuel': 'fa-gas-pump',
        'Medical': 'fa-briefcase-medical',
        'Sports': 'fa-futbol',
        'Books': 'fa-book',
        'Clothes': 'fa-tshirt',
        'Water': 'fa-tint',
        'Internet': 'fa-wifi',
        'Mobile': 'fa-mobile-alt',
        'Phone': 'fa-phone',
        'Electricity': 'fa-bolt',
        'Gas': 'fa-fire',
        'Alcohol': 'fa-wine-bottle',
        'Coffee': 'fa-coffee',
        'Snacks': 'fa-cookie-bite',
        'Laundry': 'fa-soap',
        'Cleaning': 'fa-broom',
        'Childcare': 'fa-baby',
        'Charity': 'fa-hands-helping',
        'Insurance': 'fa-shield-alt',
        'Repairs': 'fa-tools',
        'Subscriptions': 'fa-credit-card',
        'Fees': 'fa-money-check-alt',
        'ATM': 'fa-university',
        'Bank': 'fa-university',
        'Salary': 'fa-money-bill-wave',
        'Investment': 'fa-chart-line',
        'Withdrawal': 'fa-money-bill-wave',
        'Deposit': 'fa-piggy-bank',
        'Loan': 'fa-hand-holding-usd',
        'Interest': 'fa-percentage',
        'Fine': 'fa-exclamation-triangle',
        'Toll': 'fa-road',
        'Petrol': 'fa-gas-pump',
        'Veggies': 'fa-carrot',
        'Amazon': 'fa-amazon',
        'Blinkit': 'fa-shopping-basket',
        'Parking': 'fa-parking',
        'Clothes': 'fa-tshirt',
        'Water': 'fa-tint',
        'Forum Mall food': 'fa-utensils',
        'Kota Kachori': 'fa-utensils',
        'Anand Sweets': 'fa-cookie-bite',
        'Native M2 Aquaguard': 'fa-tint',
        'Westside Clothes': 'fa-tshirt',
        'Amritsari Truly North Indi...': 'fa-utensils',
        'Petrol': 'fa-gas-pump',
        'Parking': 'fa-parking',
        'Veggies': 'fa-carrot',
        'Food': 'fa-utensils',
        'Snacks': 'fa-cookie-bite',
        'Groceries': 'fa-apple-alt',
        'Shopping': 'fa-shopping-cart',
        'Travel': 'fa-plane',
        'Health': 'fa-heartbeat',
        'Utilities': 'fa-bolt',
        'Entertainment': 'fa-film',
        'General': 'fa-receipt',
        'Miscellaneous': 'fa-ellipsis-h',
    };

    // Section: Category-wise Cumulative Spends
    function updateCategoryCumulativeChart(start, end) {
        fetch(`/api/category-totals?start=${start}&end=${end}`).then(r => r.json()).then(data => {
            const ctx = document.getElementById('categoryCumulativeChart')?.getContext('2d');
            if (!ctx) return;
            if (window.categoryCumulativeChartInstance && typeof window.categoryCumulativeChartInstance.destroy === 'function') {
                window.categoryCumulativeChartInstance.destroy();
            }
            window.categoryCumulativeChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.map(c => c.category),
                    datasets: [{
                        label: 'Total Spend (INR)',
                        data: data.map(c => c.total),
                        backgroundColor: data.map((_, i) => `hsl(${i * 37 % 360}, 70%, 60%)`)
                    }]
                },
                options: {
                    indexAxis: 'y',
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    return `₹${context.parsed.x.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: {
                            ticks: {
                                callback: function(value) { return '₹' + value; }
                            }
                        }
                    }
                }
            });
        });
    }

    // Add Font Awesome for icons
    if (!document.getElementById('fa-cdn')) {
        const fa = document.createElement('link');
        fa.rel = 'stylesheet';
        fa.href = 'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.4/css/all.min.css';
        fa.id = 'fa-cdn';
        document.head.appendChild(fa);
    }

    // Patch: Add icons to month-wise expenses table
    function getCategoryIconHtml(category) {
        const icon = categoryIcons[category] || '';
        if (icon) {
            return `<i class="fas ${icon}" style="margin-right:6px;"></i>`;
        } else {
            // fallback emoji for unknown category
            return '🗂️ ';
        }
    }
}); 